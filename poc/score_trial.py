#!/usr/bin/env python3
"""Stage 3 scoring — joins trial responses against the ground-truth manifest.

Reports, per condition:
  - accuracy, and 95% Wilson interval (16 clips is small; show the uncertainty)
  - false-positive rate on the normal clips (the project's headline metric)
  - recall on the anomalous clips, overall and per bug type
  - unparseable / missing responses (counted as WRONG — a judge that fails to
    answer has not detected anything)
  - defect-type match rate for `ours` (bonus semantic metric)

Then the paired comparison: McNemar exact test on the clips where the two
conditions disagree. With n=16 this is a sanity check, not a publishable
p-value — the PoC question is "does the pipeline separate at all?".

Usage: python3 poc/score_trial.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path

POC_DIR = Path(__file__).resolve().parent
RAW_DIR = POC_DIR / "results" / "raw"
SUMMARY = POC_DIR / "results" / "summary.json"

CONDITIONS = ("baseline", "ours")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar on discordant pairs (binomial, p=0.5)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def load_results() -> dict[tuple[str, str], dict]:
    out = {}
    for f in RAW_DIR.glob("*.json"):
        if f.name.startswith("DRY__"):
            continue
        r = json.loads(f.read_text())
        out[(r["id"], r["condition"])] = r
    return out


def main() -> None:
    manifest = json.loads((POC_DIR / "manifest.json").read_text())
    results = load_results()
    if not results:
        raise SystemExit(f"No results in {RAW_DIR}. Run run_trial.py first.")

    cases = manifest["cases"]
    per_cond: dict[str, dict] = {}
    correct: dict[str, dict[str, bool]] = {c: {} for c in CONDITIONS}

    for cond in CONDITIONS:
        n = n_correct = fp = tp = fn = tn = bad = 0
        by_type: dict[str, list[int]] = {}
        type_match = type_total = 0
        for case in cases:
            r = results.get((case["id"], cond))
            if r is None:
                continue  # clip not yet run — excluded from BOTH via pairing below
            n += 1
            verdict = (r.get("verdict") or "").strip().lower()
            if verdict not in ("normal", "anomaly"):
                bad += 1
                verdict = "__invalid__"
            ok = verdict == case["ground_truth"]
            correct[cond][case["id"]] = ok
            n_correct += ok
            if case["is_anomaly"]:
                tp += ok
                fn += not ok
                by_type.setdefault(case["bug_type"], []).append(int(ok))
                if cond == "ours" and ok:
                    type_total += 1
                    type_match += r.get("defect_type") == case["bug_type"]
            else:
                tn += ok
                fp += not ok
        lo, hi = wilson(n_correct, n)
        per_cond[cond] = {
            "clips_scored": n,
            "accuracy": round(n_correct / n, 3) if n else None,
            "accuracy_ci95": [round(lo, 3), round(hi, 3)],
            "false_positive_rate": round(fp / (fp + tn), 3) if (fp + tn) else None,
            "anomaly_recall": round(tp / (tp + fn), 3) if (tp + fn) else None,
            "recall_by_bug_type": {
                k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_type.items())
            },
            "invalid_responses": bad,
            **(
                {"defect_type_match": f"{type_match}/{type_total}"}
                if cond == "ours" and type_total
                else {}
            ),
        }

    # Paired comparison over clips both conditions answered.
    paired = sorted(set(correct["baseline"]) & set(correct["ours"]))
    b = sum(1 for i in paired if correct["baseline"][i] and not correct["ours"][i])
    c = sum(1 for i in paired if not correct["baseline"][i] and correct["ours"][i])
    p = mcnemar_exact(b, c)

    summary = {
        "conditions": per_cond,
        "paired": {
            "clips_paired": len(paired),
            "baseline_only_correct": b,
            "ours_only_correct": c,
            "mcnemar_exact_p": round(p, 4),
        },
    }
    SUMMARY.write_text(json.dumps(summary, indent=2) + "\n")

    print(json.dumps(summary, indent=2))
    print(f"\nSummary written to {SUMMARY}")
    disagreements = [
        i for i in paired if correct["baseline"][i] != correct["ours"][i]
    ]
    if disagreements:
        print("\nDisagreements (inspect these first):")
        for i in disagreements:
            winner = "ours" if correct["ours"][i] else "baseline"
            print(f"  {i}: only {winner} correct")


if __name__ == "__main__":
    main()
