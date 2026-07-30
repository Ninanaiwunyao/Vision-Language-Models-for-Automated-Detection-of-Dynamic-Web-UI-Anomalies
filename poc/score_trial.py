#!/usr/bin/env python3
"""Stage 3 scoring — config-driven, multi-condition (EXP002).

Joins the cached trial responses against the ground-truth manifest and reports,
per condition and across conditions:

  Per condition (aggregated over repeats):
    - accuracy: mean +/- SD across repeats, plus a bootstrap 95% CI that
      resamples per-clip outcomes (so it does not depend on repeat variance)
    - false-positive rate on normals, recall on anomalies (overall + per type)
    - invalid / unparseable responses (counted as WRONG)
    - defect-type match rate for B1 (dual-path) conditions
    - mean tokens / latency / cost per clip

  Across conditions:
    - omnibus: Cochran's Q over the per-clip correctness matrix
    - post-hoc accuracy: pairwise exact McNemar + Holm-Bonferroni, with effect
      size (Cohen's g)
    - post-hoc efficiency: pairwise Wilcoxon signed-rank on tokens / latency /
      cost, with effect size (matched-pairs rank-biserial), Holm-corrected

The tests themselves live in `stats_tests.py`, shared with the pre-registered
power analysis. Every test records its name, n, exact/asymptotic p, and an
effect size. All of it is written to summary.json, not just printed.

Binary outcomes are collapsed across repeats by majority vote (temperature is
0.0, so repeats mainly probe residual API non-determinism).

Usage:
  python3 poc/score_trial.py
  python3 poc/score_trial.py --results-dir poc/results/exp002/raw
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

POC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(POC_DIR))

import conditions as cond  # noqa: E402
import paths  # noqa: E402
import stats_tests as st  # noqa: E402

EXPERIMENT_ID = "exp002"
DEFAULT_CONFIG = paths.frozen_config(EXPERIMENT_ID)
DEFAULT_RESULTS = paths.raw_dir(EXPERIMENT_ID)

BOOTSTRAP_N = 10_000
BOOTSTRAP_SEED = 20260728  # fixed + recorded so the CIs are reproducible

# Paid-tier price per 1M tokens (USD). Source: price_token.md (Gemini API).
PRICE_PER_1M = {
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},
}
FALLBACK_MODEL = "gemini-3.1-flash-lite"

# The per-clip continuous metrics compared between conditions.
EFFICIENCY_METRICS = ("total_tokens", "latency_s", "cost_usd")

VALID_VERDICTS = ("normal", "anomaly")


# --- data loading ----------------------------------------------------------

def load_results(results_dir: Path) -> dict[tuple[str, str, int], dict]:
    """Key raw responses by (clip_id, condition, repeat)."""
    out: dict[tuple[str, str, int], dict] = {}
    for f in results_dir.glob("*.json"):
        if f.name.startswith("DRY__"):
            continue
        r = paths.read_json(f)
        rep = int(r.get("repeat", 1))
        out[(r["id"], r["condition"], rep)] = r
    return out


def price_for(model: str) -> tuple[dict, bool]:
    if model in PRICE_PER_1M:
        return PRICE_PER_1M[model], False
    return PRICE_PER_1M[FALLBACK_MODEL], True


def call_cost(usage: dict | None, price: dict) -> float | None:
    if not usage:
        return None
    pt = usage.get("prompt_tokens") or 0
    ct = usage.get("candidates_tokens") or 0
    return pt / 1e6 * price["input"] + ct / 1e6 * price["output"]


def verdict_of(resp: dict) -> str:
    return (resp.get("verdict") or "").strip().lower()


def verdict_correct(resp: dict, truth: str) -> bool:
    # Invalid/unparseable verdicts are simply != truth -> counted wrong.
    return verdict_of(resp) == truth


def is_invalid(resp: dict) -> bool:
    return verdict_of(resp) not in VALID_VERDICTS


# --- per-condition scoring -------------------------------------------------

@dataclass
class ConditionScore:
    """One condition's contribution: its own summary block, plus the per-clip
    vectors that the paired across-condition tests consume.

    `correct` is the majority vote per clip (the unit the binary paired tests
    work on); `correct_frac` is the fraction of repeats correct, which is what
    the accuracy bootstrap resamples so its CI is coherent with accuracy_mean
    rather than with the collapsed vote.
    """

    summary: dict = field(default_factory=dict)
    correct: dict[str, bool] = field(default_factory=dict)
    correct_frac: dict[str, float] = field(default_factory=dict)
    continuous: dict[str, dict[str, float | None]] = field(default_factory=dict)


def _repeat_accuracies(code: str, cases: dict, results: dict,
                       repeats_seen: list[int]) -> list[float]:
    """Accuracy of each repeat separately — the basis of the mean +/- SD."""
    accs: list[float] = []
    for rep in repeats_seen:
        got = correct = 0
        for cid, case in cases.items():
            r = results.get((cid, code, rep))
            if r is None:
                continue
            got += 1
            correct += verdict_correct(r, case["ground_truth"])
        if got:
            accs.append(correct / got)
    return accs


def _clip_continuous(reps: list[dict], price: dict) -> dict[str, float | None]:
    """Per-clip mean of each efficiency metric over the repeats that reported it."""
    toks = [r["usage"]["total_tokens"] for r in reps
            if r.get("usage") and r["usage"].get("total_tokens")]
    lats = [r["latency_s"] for r in reps if r.get("latency_s") is not None]
    costs = [c for c in (call_cost(r.get("usage"), price) for r in reps)
             if c is not None]
    return {
        "total_tokens": float(np.mean(toks)) if toks else None,
        "latency_s": float(np.mean(lats)) if lats else None,
        "cost_usd": float(np.mean(costs)) if costs else None,
    }


def score_condition(code: str, cases: dict, results: dict,
                    repeats_seen: list[int], price: dict) -> ConditionScore:
    is_b1 = code.endswith("B1")
    out = ConditionScore()

    rep_acc = _repeat_accuracies(code, cases, results, repeats_seen)

    invalid = 0
    fp = tn = tp = fn = 0
    by_type: dict[str, list[int]] = {}
    type_match = type_total = 0

    for cid, case in cases.items():
        reps = [r for r in (results.get((cid, code, rep)) for rep in repeats_seen)
                if r is not None]
        if not reps:
            continue
        oks = [verdict_correct(r, case["ground_truth"]) for r in reps]
        invalid += sum(1 for r in reps if is_invalid(r))

        ok = sum(oks) >= len(oks) / 2  # majority vote (>=half)
        out.correct[cid] = ok
        out.correct_frac[cid] = sum(oks) / len(oks)
        out.continuous[cid] = _clip_continuous(reps, price)

        if case["is_anomaly"]:
            tp += ok
            fn += not ok
            by_type.setdefault(case["bug_type"], []).append(int(ok))
            if is_b1 and ok:
                type_total += 1
                # defect_type match on the majority verdict's repeats
                dts = [r.get("defect_type") for r in reps]
                type_match += sum(1 for d in dts if d == case["bug_type"]) >= len(dts) / 2
        else:
            tn += ok
            fp += not ok

    clip_fracs = list(out.correct_frac.values())
    acc_mean = float(np.mean(rep_acc)) if rep_acc else None
    acc_sd = float(np.std(rep_acc, ddof=1)) if len(rep_acc) > 1 else 0.0

    def cont_mean(metric: str) -> float | None:
        vals = [d[metric] for d in out.continuous.values() if d[metric] is not None]
        return round(float(np.mean(vals)), 4) if vals else None

    out.summary = {
        "clips_scored": len(clip_fracs),
        "accuracy_mean": round(acc_mean, 4) if acc_mean is not None else None,
        "accuracy_sd": round(acc_sd, 4),
        "accuracy_ci95_bootstrap": st.bootstrap_ci(clip_fracs, BOOTSTRAP_N, BOOTSTRAP_SEED),
        "false_positive_rate": round(fp / (fp + tn), 4) if (fp + tn) else None,
        "anomaly_recall": round(tp / (tp + fn), 4) if (tp + fn) else None,
        "recall_by_bug_type": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_type.items())},
        "invalid_responses": invalid,
        "mean_total_tokens": cont_mean("total_tokens"),
        "mean_latency_s": cont_mean("latency_s"),
        "mean_cost_usd": cont_mean("cost_usd"),
        **({"defect_type_match": f"{type_match}/{type_total}"} if is_b1 and type_total else {}),
    }
    return out


# --- across-condition tests ------------------------------------------------

def omnibus(codes: list[str], scores: dict[str, ConditionScore]) -> dict | None:
    """Cochran's Q over the clips that were answered under EVERY condition."""
    if len(codes) < 2:
        return None
    common = sorted(set.intersection(*(set(scores[c].correct) for c in codes)))
    matrix = [[int(scores[c].correct[cid]) for c in codes] for cid in common]
    return st.cochrans_q(matrix) if matrix else None


def posthoc_accuracy(codes: list[str], scores: dict[str, ConditionScore]) -> list[dict]:
    """Pairwise exact McNemar on the discordant pairs, Holm-corrected."""
    rows: list[dict] = []
    for a, b in itertools.combinations(codes, 2):
        ca, cb = scores[a].correct, scores[b].correct
        paired = sorted(set(ca) & set(cb))
        n_b = sum(1 for i in paired if ca[i] and not cb[i])
        n_c = sum(1 for i in paired if not ca[i] and cb[i])
        rows.append({
            "pair": f"{a} vs {b}", "test": "exact McNemar",
            "n_paired": len(paired), "n_discordant": n_b + n_c,
            f"{a}_only_correct": n_b, f"{b}_only_correct": n_c,
            "p": round(st.mcnemar_exact(n_b, n_c), 6),
            "effect_size_cohens_g": st.cohens_g(n_b, n_c),
        })
    if rows:
        st.holm_bonferroni(rows)
    return rows


def posthoc_efficiency(codes: list[str],
                       scores: dict[str, ConditionScore]) -> dict[str, list[dict]]:
    """Pairwise Wilcoxon signed-rank per efficiency metric, Holm-corrected."""
    out: dict[str, list[dict]] = {}
    for metric in EFFICIENCY_METRICS:
        rows: list[dict] = []
        for a, b in itertools.combinations(codes, 2):
            da, db = scores[a].continuous, scores[b].continuous
            paired = sorted(set(da) & set(db))
            keep = [(da[i][metric], db[i][metric]) for i in paired
                    if da[i][metric] is not None and db[i][metric] is not None]
            if not keep:
                continue
            xa = [u for u, _ in keep]
            xb = [v for _, v in keep]
            rows.append({"pair": f"{a} vs {b}", **st.wilcoxon_signed_rank(xa, xb),
                         f"{a}_median": round(float(np.median(xa)), 4),
                         f"{b}_median": round(float(np.median(xb)), 4)})
        if rows:
            st.holm_bonferroni(rows)
        out[metric] = rows
    return out


# --- entry point -----------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    ap.add_argument("--manifest", type=Path, default=paths.MANIFEST_JSON)
    return ap.parse_args()


def main() -> None:
    args = parse_args()

    codes = cond.load(args.config).conditions
    manifest = paths.read_json(args.manifest)
    cases = {c["id"]: c for c in manifest["cases"]}
    results = load_results(args.results_dir)
    if not results:
        raise SystemExit(f"No results in {args.results_dir}. Run run_trial.py first.")

    model = next(iter(results.values())).get("model", FALLBACK_MODEL)
    price, price_assumed = price_for(model)
    repeats_seen = sorted({key[2] for key in results})

    scores = {code: score_condition(code, cases, results, repeats_seen, price)
              for code in codes}

    summary = {
        "experiment": "EXP002",
        "model": model,
        "price_per_1M_usd": price,
        "price_assumed": price_assumed,
        "conditions": codes,
        "repeats_seen": repeats_seen,
        "bootstrap": {"n": BOOTSTRAP_N, "seed": BOOTSTRAP_SEED},
        "per_condition": {code: scores[code].summary for code in codes},
        "omnibus_cochran_q": omnibus(codes, scores),
        "posthoc_accuracy_mcnemar_holm": posthoc_accuracy(codes, scores),
        "posthoc_efficiency_wilcoxon_holm": posthoc_efficiency(codes, scores),
    }
    out_path = args.results_dir.parent / "summary.json"
    paths.write_json(out_path, summary)

    print(json.dumps(summary, indent=2))
    print(f"\nSummary written to {out_path}")


if __name__ == "__main__":
    main()
