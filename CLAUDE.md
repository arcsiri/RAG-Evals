# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

The project uses `uv` (implied by `uv.lock`) and Python 3.11+.

### Running Evaluations
The evaluation suite is the primary driver of this repository. It measures a RAG pipeline across quality, safety, and operational dimensions.

- **Run Full Eval Suite (Candidate):** 
  `python -m evals.run_suite`
  Writes results to `baselines/candidate.json`.

- **Bless as Baseline:**
  `python -m evals.run_suite --baseline`
  Writes results to `baselines/baseline.json`.

- **Run with Metadata:**
  `python -m evals.run_suite --label "description of change" --full`
  `--full` includes detailed "info" metrics (avg/min/max/n) in the snapshot.

- **Compare Snapshots:**
  `python -m evals.compare`
  Compares `baselines/baseline.json` and `baselines/candidate.json`.
  Returns exit codes: 0 (PASS), 1 (FAIL), 2 (REVIEW).

### Other Commands
- **General Entrypoint:** `python main.py` (Currently a placeholder)
- **Environment:** Ensure `.env` is configured for OpenAI/Anthropic API keys.

## Architecture

The repository implements a regression-testing framework for RAG (Retrieval-Augmented Generation) pipelines.

### Core Components (`src/`)
- `rag_pipeline.py`: Orchestrates the flow from query $\rightarrow$ retrieval $\rightarrow$ generation.
- `retriever.py` & `reranker.py`: Handle document fetching and re-ordering.
- `generator.py`: LLM-based answer generation using a specific prompt.

### Evaluation Framework (`evals/`)
The evaluation is structured as a hierarchy of tests:
1. **Component Evals:** `eval_retriever.py` and `eval_generator.py` test pieces in isolation.
2. **Pipeline Evals:** `eval_rag_pipeline.py` tests the integrated triad (Query, Context, Answer).
3. **Application Evals:** `eval_application.py` tests end-to-end behavior.
4. **Guardrails:** `eval_safety.py` and `eval_ops.py` (latency, cost) enforce hard constraints.

**Metric Flow:**
`run_suite.py` $\rightarrow$ runs all evals $\rightarrow$ flattens metrics into a dotted ID space (`namespace.metric.stat`) $\rightarrow$ writes a JSON snapshot.

### Decision Logic (`evals/compare.py` & `evals/metric_registry.py`)
The framework uses a **Metric Registry** to define:
- **Kind:** `gate` (must not regress), `guardrail` (regression requires review), or `info` (purely descriptive).
- **Direction:** `higher` or `lower` is better.
- **Tolerance:** Absolute and relative thresholds to filter out noise.

**Verdicts:**
- `PASS`: No gates blocked, no guardrails regressed beyond tolerance.
- `REVIEW`: A guardrail regressed; requires human intervention.
- `FAIL`: A gate regressed; automatically blocks promotion.
