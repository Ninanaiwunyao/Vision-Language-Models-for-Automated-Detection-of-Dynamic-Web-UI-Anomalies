#!/usr/bin/env python3
"""Stage 1 — Build the PoC ground-truth manifest.

Every evaluation in this project scores the model's verdict against this file,
so the manifest is the experiment's foundation. It is generated *from the
dataset itself* (<dataset>/metadata.json), which was in turn produced by the
Playwright fault-injection matrix in latest/tests/poc-bug-injection.spec.ts.

Because every bug is self-injected via a known CSS mutation, the ground-truth
label and the expected symptom are *known a priori* — they are derived here
from the bug key, never hand-written per video. That keeps the manifest
objective and trivially reproducible.

Output (written next to this script, in poc/):
  - manifest.json : rich, machine-readable ground truth (one object per video)
  - manifest.csv  : flat view for quick inspection / spreadsheets

Two selection modes:
  - pretrial : the original balanced 16-clip subset (8 normal / 8 anomalous, the
               two hardest types sampled twice). Used for the PoC pre-trial and
               kept for reproducibility.
  - full     : every clip in the dataset, no subsetting. This is what the frozen
               EXP001 spec requires for dataset_v3 ("all 120 clips are evaluated";
               N_CONTROLS / BUG_SELECTION subsetting is bypassed so subset
               selection is no longer an experimental variable).

The mode defaults to `full` for dataset_v3 and any later dataset, and to
`pretrial` for dataset_v1 / dataset_v2; override with --full / --pretrial.

Usage:
  python3 poc/build_manifest.py                 # newest dataset, default mode
  python3 poc/build_manifest.py dataset_v3      # -> full (120 clips)
  python3 poc/build_manifest.py dataset_v2 --pretrial
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

POC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(POC_DIR))

import paths  # noqa: E402
from paths import MANIFEST_CSV, MANIFEST_JSON, REPO_ROOT  # noqa: E402

# Newest first: the default dataset is the latest one present on disk.
KNOWN_DATASETS = ("dataset_v3", "dataset_v2", "dataset_v1")

# Datasets whose default selection mode is the 16-clip pre-trial subset. Anything
# not listed here (dataset_v3 and later) defaults to the full formal-trial set.
PRETRIAL_DEFAULT_DATASETS = {"dataset_v1", "dataset_v2"}

# ---------------------------------------------------------------------------
# Expected symptom per bug type — the "known answer" auto-derived from the CSS
# defect that the Playwright matrix injects. Keyed on bugKey so it is applied
# uniformly to every video of that type; nothing here is written by hand for an
# individual clip. The grader uses `expectation` for semantic matching and
# `ground_truth` ("normal"/"anomaly") for the binary detection metric.
# ---------------------------------------------------------------------------
EXPECTED_SYMPTOM: dict[str, str] = {
    "skew_distortion": (
        "The whole to-do app is geometrically distorted (skewed, rotated and "
        "scaled), so the interface looks visibly broken/misaligned."
    ),
    "blur_unreadable": (
        "The whole app is heavily blurred and its text is unreadable."
    ),
    "vanish_invisible": (
        "The app content is invisible/blank (rendered fully transparent), so "
        "the UI appears to be missing."
    ),
    "flyaway_offset": (
        "The app is shifted far from its normal position (partly or fully "
        "off-screen) — a massive, obvious layout shift."
    ),
    "flicker_seizure": (
        "UI elements flicker/strobe rapidly between two conflicting coloured "
        "states."
    ),
    "displaynone_collapse": (
        "A key section is missing (removed from the layout), so the content "
        "below it collapses and jumps upward."
    ),
}

NORMAL_EXPECTATION = (
    "The app renders and behaves normally; no visual defect should be reported."
)

# ---------------------------------------------------------------------------
# PoC subset selection.
#   controls: first N control clips.
#   bugs:     first `count` clips of each bug type.
# Chosen so the subset is balanced (8 vs 8), stays inside 10-20, and covers
# every bug type. The two lowest-diff / hardest types (blur, skew) are sampled
# twice so the hardest cases are not represented by a single clip.
# ---------------------------------------------------------------------------
N_CONTROLS = 8
BUG_SELECTION: dict[str, int] = {
    "skew_distortion": 2,   # subtle geometric change — hardest, sample twice
    "blur_unreadable": 2,   # lowest pixel-diff of all types — sample twice
    "vanish_invisible": 1,
    "flyaway_offset": 1,
    "flicker_seizure": 1,
    "displaynone_collapse": 1,
}  # -> 8 buggy clips

# Stable presentation order for the anomaly types in the full manifest. Keyed on
# the same bugKeys the injection matrix emits; also the authoritative set of
# types the full mode requires every dataset to contain.
BUG_ORDER: dict[str, int] = {key: i for i, key in enumerate(EXPECTED_SYMPTOM)}

CSV_FIELDS = [
    "id",
    "ground_truth",
    "is_anomaly",
    "bug_type",
    "bug_label",
    "expectation",
    "video_path",
    "diff_ratio",
    "min_diff_ratio",
    "target_url",
    "browser",
]


# ---------------------------------------------------------------------------
# What to build. Resolved once from the CLI and then threaded through as an
# argument, so nothing in this module depends on global state or on argv having
# been parsed at import time.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BuildSpec:
    dataset_dir: Path
    mode: str  # "full" | "pretrial"

    @property
    def dataset_name(self) -> str:
        return self.dataset_dir.name

    @property
    def metadata_path(self) -> Path:
        return self.dataset_dir / "metadata.json"


def default_dataset() -> Path:
    for name in KNOWN_DATASETS:
        if (REPO_ROOT / name / "metadata.json").exists():
            return REPO_ROOT / name
    raise SystemExit("No dataset with metadata.json found in the repo root")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Build the ground-truth manifest.")
    ap.add_argument("dataset", nargs="?", default=None,
                    help="dataset dir name (default: newest present)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true",
                      help="include every clip (formal trial, e.g. EXP001)")
    mode.add_argument("--pretrial", action="store_true",
                      help="balanced 16-clip pre-trial subset")
    return ap.parse_args()


def resolve_spec(args: argparse.Namespace) -> BuildSpec:
    """Explicit flag wins; otherwise fall back to the per-dataset default
    (pre-trial for v1/v2, full for v3+)."""
    dataset_dir = REPO_ROOT / args.dataset if args.dataset else default_dataset()
    if args.full:
        mode = "full"
    elif args.pretrial:
        mode = "pretrial"
    else:
        mode = ("pretrial" if dataset_dir.name in PRETRIAL_DEFAULT_DATASETS
                else "full")
    return BuildSpec(dataset_dir=dataset_dir, mode=mode)


# --- selection --------------------------------------------------------------

def load_cases(spec: BuildSpec) -> list[dict]:
    if not spec.metadata_path.exists():
        raise SystemExit(f"Dataset metadata not found: {spec.metadata_path}")
    return paths.read_json(spec.metadata_path)["cases"]


def numeric_suffix(case_id: str) -> int:
    """Sort key on the trailing integer so _2 does not sort before _10 wrongly
    (all our ids are _1.._10, but this keeps ordering sane and stable)."""
    return int(case_id.rsplit("_", 1)[-1])


def select_pretrial(spec: BuildSpec, cases: list[dict]) -> list[dict]:
    """The original balanced 16-clip subset (8 normal / 8 anomalous)."""
    controls = sorted(
        (c for c in cases if c["group"] == "control"), key=lambda c: numeric_suffix(c["id"])
    )
    chosen: list[dict] = controls[:N_CONTROLS]

    for bug_key, count in BUG_SELECTION.items():
        matching = sorted(
            (c for c in cases if c.get("bugKey") == bug_key),
            key=lambda c: numeric_suffix(c["id"]),
        )
        if len(matching) < count:
            raise SystemExit(
                f"Requested {count} clips of '{bug_key}' but only {len(matching)} exist"
            )
        chosen.extend(matching[:count])

    # Guard the two headline requirements before we emit anything.
    n = len(chosen)
    n_anom = sum(1 for c in chosen if c["group"] == "bug")
    if not (10 <= n <= 20):
        raise SystemExit(f"Subset size {n} is outside the required 10-20 range")
    if abs(n_anom - (n - n_anom)) > 2:
        raise SystemExit(f"Subset is not balanced: {n_anom} anomalous vs {n - n_anom} normal")
    return chosen


def select_full(spec: BuildSpec, cases: list[dict]) -> list[dict]:
    """Every clip in the dataset — the formal-trial set (e.g. EXP001).

    No subsetting: controls first, then anomalies grouped by type in the
    injection matrix's order, each block sorted by clip number. We still guard
    the properties the study relies on — a non-empty, roughly balanced set with
    all six bug types present — but impose no size cap."""
    controls = sorted(
        (c for c in cases if c["group"] == "control"),
        key=lambda c: numeric_suffix(c["id"]),
    )
    bugs = sorted(
        (c for c in cases if c["group"] == "bug"),
        # Unknown bug keys sort last rather than crashing the sort key.
        key=lambda c: (BUG_ORDER.get(c.get("bugKey"), len(BUG_ORDER)), numeric_suffix(c["id"])),
    )
    chosen = controls + bugs

    if not chosen:
        raise SystemExit(f"{spec.dataset_name} contains no cases")
    present = {c.get("bugKey") for c in bugs}
    missing = set(BUG_ORDER) - present
    if missing:
        raise SystemExit(
            f"{spec.dataset_name} is missing required bug types: {sorted(missing)}"
        )
    unknown = present - set(BUG_ORDER)
    if unknown:
        raise SystemExit(
            f"{spec.dataset_name} has bug types with no expected-symptom mapping: {sorted(unknown)}"
        )
    n_anom, n_norm = len(bugs), len(controls)
    # Not a hard failure — just refuse a wildly lopsided set the metrics can't
    # carry (FPR and recall are meant to have comparable uncertainty).
    if n_norm == 0 or n_anom == 0 or max(n_anom, n_norm) > 3 * min(n_anom, n_norm):
        raise SystemExit(
            f"{spec.dataset_name} is too imbalanced for a paired trial: "
            f"{n_anom} anomalous vs {n_norm} normal"
        )
    return chosen


def select_cases(spec: BuildSpec, cases: list[dict]) -> list[dict]:
    picker = select_full if spec.mode == "full" else select_pretrial
    return picker(spec, cases)


# --- entry building ---------------------------------------------------------

def verify_assets(spec: BuildSpec, case_path: str) -> dict[str, str | None]:
    """Confirm the video + keyframes exist and return their repo-relative paths.
    A manifest that points at missing files is worse than useless, so we fail
    loudly here rather than at evaluation time."""
    assets: dict[str, str | None] = {
        "video": f"{spec.dataset_name}/{case_path}/video.webm",
        "normal_frame": f"{spec.dataset_name}/{case_path}/normal.png",
        "buggy_frame": f"{spec.dataset_name}/{case_path}/buggy.png",
        "diff_frame": f"{spec.dataset_name}/{case_path}/diff.png",
    }
    for name, rel in assets.items():
        if not (REPO_ROOT / rel).exists():
            raise SystemExit(f"Missing {name} for {case_path}: {rel}")
    # DOM mutation trace: present from dataset_v2 onward, optional before.
    trace = f"{spec.dataset_name}/{case_path}/domtrace.json"
    assets["dom_trace"] = trace if (REPO_ROOT / trace).exists() else None
    return assets


def to_entry(spec: BuildSpec, case: dict) -> dict:
    is_anomaly = case["group"] == "bug"
    bug_key = case.get("bugKey")
    assets = verify_assets(spec, case["path"])
    return {
        "id": case["id"],
        "ground_truth": "anomaly" if is_anomaly else "normal",
        "is_anomaly": is_anomaly,
        "bug_type": bug_key,
        "bug_label": case.get("bugLabel"),
        "expectation": EXPECTED_SYMPTOM[bug_key] if is_anomaly else NORMAL_EXPECTATION,
        "video_path": assets["video"],
        "dom_trace_path": assets["dom_trace"],
        "keyframes": {
            "normal": assets["normal_frame"],
            "buggy": assets["buggy_frame"],
            "diff": assets["diff_frame"],
        },
        "diff_ratio": round(case.get("diffRatio", 0.0), 6),
        "min_diff_ratio": case.get("minDiffRatio", 0.0),
        "target_url": case.get("targetUrl"),
        "browser": case.get("browser"),
    }


# --- output -----------------------------------------------------------------

def write_manifest_json(spec: BuildSpec, entries: list[dict]) -> None:
    n_anom = sum(1 for e in entries if e["is_anomaly"])
    if spec.mode == "full":
        description = (
            f"Ground-truth manifest — full {spec.dataset_name} "
            f"({len(entries)} clips, no subsetting)."
        )
    else:
        description = (
            f"PoC pre-trial ground-truth manifest (balanced subset of {spec.dataset_name})."
        )
    paths.write_json(MANIFEST_JSON, {
        "schema_version": 2,
        "description": description,
        "selection_mode": spec.mode,
        "source": f"{spec.dataset_name}/metadata.json",
        "total": len(entries),
        "normal": len(entries) - n_anom,
        "anomaly": n_anom,
        "cases": entries,
    })


def write_manifest_csv(entries: list[dict]) -> None:
    with MANIFEST_CSV.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for e in entries:
            writer.writerow(e)


def report(spec: BuildSpec, entries: list[dict]) -> None:
    n_anom = sum(1 for e in entries if e["is_anomaly"])
    print(f"Manifest written [{spec.mode}] from {spec.dataset_name}: {len(entries)} videos "
          f"({len(entries) - n_anom} normal / {n_anom} anomalous)")
    print(f"  - {MANIFEST_JSON.relative_to(REPO_ROOT)}")
    print(f"  - {MANIFEST_CSV.relative_to(REPO_ROOT)}")
    by_type: dict[str, int] = {}
    for e in entries:
        key = e["bug_type"] or "normal"
        by_type[key] = by_type.get(key, 0) + 1
    print("  breakdown:", ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))


def main() -> None:
    spec = resolve_spec(parse_args())
    entries = [to_entry(spec, c) for c in select_cases(spec, load_cases(spec))]
    write_manifest_json(spec, entries)
    write_manifest_csv(entries)
    report(spec, entries)


if __name__ == "__main__":
    main()
