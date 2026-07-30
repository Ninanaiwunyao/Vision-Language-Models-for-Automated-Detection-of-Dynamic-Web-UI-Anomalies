"""Repository paths and JSON helpers shared by every pipeline script.

Everything is resolved from this file's location rather than the working
directory, so a script behaves identically whether it is invoked as
`python3 pipeline/run_trial.py` from the repo root or by an absolute path from
anywhere else.

The layout keeps three things apart because they have different lifecycles:

    pipeline/   code — refactorable at any time
    data/       ground truth derived from a dataset (manifest.json/.csv)
    results/    per-experiment model outputs — evidence, effectively append-only

The datasets themselves (the .webm/.png binaries) stay out of git; the root
.gitignore explains why.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parent

EXPERIMENTS_DIR = REPO_ROOT / "experiments"
RESULTS_DIR = REPO_ROOT / "results"
DATA_DIR = REPO_ROOT / "data"

MANIFEST_JSON = DATA_DIR / "manifest.json"
MANIFEST_CSV = DATA_DIR / "manifest.csv"


def frozen_config(experiment_id: str) -> Path:
    """The frozen spec of an experiment, e.g. experiments/exp002/config.frozen.json."""
    return EXPERIMENTS_DIR / experiment_id / "config.frozen.json"


def results_dir(experiment_id: str) -> Path:
    """Where one experiment's outputs live (summary + the raw response cache)."""
    return RESULTS_DIR / experiment_id


def raw_dir(experiment_id: str) -> Path:
    """The per-call response cache that makes a run resumable."""
    return results_dir(experiment_id) / "raw"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, doc: Any) -> None:
    """Write indented JSON with a trailing newline — the format every result
    file in this repo already uses, so regenerated files stay diff-friendly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")
