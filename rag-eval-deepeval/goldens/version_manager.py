import os
import json

CONFIG_PATH = "rag-eval-deepeval/goldens/config.json"
VERSIONS_ROOT = "rag-eval-deepeval/goldens/versions"

class VersionManager:
    def __init__(self, override_version=None):
        self.config = self._load_config()
        self.override_version = override_version

    def _load_config(self):
        if not os.path.exists(CONFIG_PATH):
            raise FileNotFoundError(f"Golden dataset config not found at {CONFIG_PATH}")
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)

    def resolve_path(self, dataset_name):
        """
        Resolves the filesystem path for a dataset.
        Priority:
        1. CLI override version
        2. Config active_version for that dataset
        3. Global version fallback
        """
        version = self.override_version

        if not version:
            version = self.config.get("active_versions", {}).get(dataset_name)

        if not version:
            version = self.config.get("global_version")

        if not version:
            raise ValueError(f"Could not resolve version for dataset: {dataset_name}")

        path = os.path.join(VERSIONS_ROOT, version, f"{dataset_name}.json")

        if not os.path.exists(path):
            raise FileNotFoundError(f"Dataset version {version} for {dataset_name} not found at {path}")

        return path

    def get_current_versions(self):
        """Returns a mapping of dataset -> version used."""
        # This is a bit tricky because we don't know all datasets until we try to resolve them.
        # We can derive it from the active_versions config, adjusted by override.
        versions = self.config.get("active_versions", {}).copy()
        if self.override_version:
            for k in versions:
                versions[k] = self.override_version
        return versions
