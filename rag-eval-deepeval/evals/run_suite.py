# evals/run_suite.py
"""
run_suite.py -- the orchestrator.

Runs the FULL eval suite (quality + safety + operational) against ONE pipeline,
flattens every metric into a dotted id, stamps run metadata, and writes a
snapshot JSON. That snapshot is the atomic unit regression testing compares.

THE key design point: build the pipeline ONCE and inject it into every eval, so
the whole snapshot is provably the measurement of a single pipeline -- not six
independently-constructed ones that might quietly differ. This is what later lets
us say "baseline and candidate differ by exactly one change, nothing else."

    python -m evals.run_suite                          -> baselines/candidate.json
    python -m evals.run_suite --baseline               -> baselines/baseline.json
    python -m evals.run_suite --out path.json          -> custom output path
    python -m evals.run_suite --label "k=10 + reranker"  (describe the change)
    python -m evals.run_suite --quiet                  -> suppress ops/safety chatter
"""

import argparse
import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from src.rag_pipeline import RagPipeline
from src.reranker import RerankingRetriever

from evals import (
    eval_retriever,
    eval_generator,
    eval_rag_pipeline,
    eval_application,
    eval_safety,
    eval_ops,
)
from evals.metric_registry import rule_for
from goldens.version_manager import VersionManager


load_dotenv()


# ============================================================
# 1. CONFIG
# ============================================================
BASELINE_PATH  = "baselines/baseline.json"
CANDIDATE_PATH = "baselines/candidate.json"


# ============================================================
# 2. FLATTENING  (every eval -> dotted metric ids)
# ============================================================
# Quality evals return NESTED {metric_name: {stat: val}} (from summarize_by_metric).
# ops/safety return ALREADY-FLAT {"sub.stat": val}. We normalize both to a single
# 3-level id space: "<namespace>.<source>.<stat>", e.g.
#   retriever.contextual_recall.pass_rate
#   safety.scope.pass_rate
#   ops.latency.e2e_p95_ms
# These strings are the metric ids the registry + compare.py will key on.
def _slug(name):
    return name.strip().lower().replace(" ", "_")


def flatten_nested(namespace, summary):
    """{metric: {stat: val}} -> {'namespace.metric_slug.stat': val}."""
    out = {}
    for metric, stats in summary.items():
        slug = _slug(metric)
        for stat, val in stats.items():
            out[f"{namespace}.{slug}.{stat}"] = val
    return out


def prefix_flat(namespace, flat):
    """{'sub.stat': val} -> {'namespace.sub.stat': val}."""
    return {f"{namespace}.{key}": val for key, val in flat.items()}


# ============================================================
# 3. RUN METADATA  (provenance -- what produced these numbers)
# ============================================================
def _git_sha():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


def _prompt_hash():
    # Hash the generator prompt so a silent prompt edit shows up as a new hash.
    try:
        from src.generator import prompt
        return hashlib.sha256(str(prompt).encode()).hexdigest()[:12]
    except Exception:
        return "unknown"


def build_metadata(label, goldens_versions=None):
    return {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "prompt_hash": _prompt_hash(),
        "label": label,          # human note: what change this snapshot represents
        "goldens_versions": goldens_versions or {},
    }


# ============================================================
# 4. THE SUITE
# ============================================================
def run_suite(label="", quiet=False, full=False):
    """Build one pipeline, run every eval against it, return a snapshot dict.

    By default the snapshot keeps only what drives a decision -- gates and
    guardrails (pass rates + headline operational numbers). The distribution
    stats (avg/min/max/n) and secondary operational numbers are 'info' in the
    registry; they're written only with full=True. The registry is the single
    source of truth: the same rules that decide the verdict decide what we
    persist.
    """
    verbose = not quiet
    t_start = time.perf_counter()

    # --- build ONCE, inject everywhere ---
    print("Building pipeline (once)...")
    rag = RagPipeline()
    # The retriever eval should measure the SAME retriever the pipeline uses, not
    # a fresh copy. Fall back to constructing one only if the pipeline doesn't
    # expose it.
    retriever = getattr(rag, "retriever", None) or RerankingRetriever()

    metrics = {}

    # --- QUALITY (component -> pipeline -> application) ---
    print("\n[1/6] retriever eval...")
    metrics.update(flatten_nested("retriever", eval_retriever.run(retriever)))

    print("\n[2/6] generator eval...")            # no pipeline: tests generate() on golden context
    metrics.update(flatten_nested("generator", eval_generator.run()))

    print("\n[3/6] pipeline (triad) eval...")
    metrics.update(flatten_nested("pipeline", eval_rag_pipeline.run(rag)))

    print("\n[4/6] application quality eval...")
    metrics.update(flatten_nested("application", eval_application.run(rag)))

    # --- SAFETY (hard gates) ---
    print("\n[5/6] safety evals...")
    metrics.update(prefix_flat("safety", eval_safety.run_safety(rag, verbose=verbose)))

    # --- OPERATIONAL ---
    print("\n[6/6] operational evals...")
    metrics.update(prefix_flat("ops", eval_ops.run_ops(rag, verbose=verbose)))

    elapsed = time.perf_counter() - t_start

    # --- Streak Calculation & Escalation ---
    streak_state = load_streak_state()
    try:
        with open(BASELINE_PATH, "r") as f:
            baseline = json.load(f)
        baseline_metrics = baseline.get("metrics", {})
    except (FileNotFoundError, json.JSONDecodeError):
        baseline_metrics = {}

    # we use the logic from compare.classify to determine regressions
    def _is_num(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool) \
            and not (isinstance(x, float) and math.isnan(x))

    for mid, val in metrics.items():
        rule = rule_for(mid)
        if rule["kind"] != "guardrail":
            continue

        bv = baseline_metrics.get(mid)
        streak = streak_state.get(mid, 0)
        if bv is None:
            continue

        regressed = False
        if rule.get("bool") or isinstance(bv, bool) or isinstance(val, bool):
            if not (bv == val or (not bv and val)):
                regressed = True
        elif _is_num(bv) and _is_num(val):
            delta = val - bv
            higher_better = rule["direction"] == "higher"
            improved = (delta > 0) if higher_better else (delta < 0)
            if not improved:
                worse = abs(delta)
                tolerance = max(rule["tol"], rule["rel_tol"] * abs(bv))
                if worse > tolerance:
                    regressed = True

        if regressed:
            streak += 1
        else:
            streak = 0
        streak_state[mid] = streak

    save_streak_state(streak_state)

    # Trim to what matters unless --full: drop 'info' metrics (avg/min/max/n,
    # secondary latency/cost) and keep gates + guardrails. ~88 metrics -> ~22.
    if not full:
        metrics = {mid: v for mid, v in metrics.items()
                   if rule_for(mid)["kind"] != "info"}

    snapshot = {
        "metadata": {
            **build_metadata(label),
            "suite_seconds": round(elapsed, 1),
            "full": full,
            "n_metrics": len(metrics),
            "streaks": streak_state,
        },
        "metrics": metrics,
    }
    return snapshot


# ============================================================
# 5. WRITE + REPORT
# ============================================================
def load_streak_state():
    path = "baselines/streak_state.json"
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

def save_streak_state(state):
    path = "baselines/streak_state.json"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)

def write_snapshot(snapshot, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2, sort_keys=True)
    return path


def print_snapshot(snapshot):
    meta = snapshot["metadata"]
    metrics = snapshot["metrics"]
    print("\n" + "=" * 74)
    print("SNAPSHOT")
    print("=" * 74)
    print(f"label       : {meta.get('label') or '(none)'}")
    print(f"created_at  : {meta['created_at']}")
    print(f"git_sha     : {meta['git_sha']}    prompt_hash: {meta['prompt_hash']}")
    print(f"suite_time  : {meta['suite_seconds']}s     metrics: {len(metrics)}")
    print("-" * 74)
    for key in sorted(metrics):
        val = metrics[key]
        shown = f"{val:.4f}" if isinstance(val, float) else str(val)
        print(f"  {key:<44} {shown}")
    print("=" * 74)


# ============================================================
# 6. ENTRYPOINT
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Run the full eval suite and write a snapshot.")
    parser.add_argument("--baseline", action="store_true",
                        help=f"write to {BASELINE_PATH} (bless this run as the baseline)")
    parser.add_argument("--out", default=None, help="custom output path")
    parser.add_argument("--label", default="", help="describe the change this snapshot represents")
    parser.add_argument("--quiet", action="store_true", help="suppress per-eval ops/safety chatter")
    parser.add_argument("--full", action="store_true",
                        help="also write info metrics (avg/min/max/n, secondary latency/cost)")
    args = parser.parse_args()

    out = args.out or (BASELINE_PATH if args.baseline else CANDIDATE_PATH)

    snapshot = run_suite(label=args.label, quiet=args.quiet, full=args.full)
    print_snapshot(snapshot)
    path = write_snapshot(snapshot, out)
    print(f"\nwrote snapshot -> {path}")


if __name__ == "__main__":
    main()