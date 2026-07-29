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

Every test records its name, n, exact/asymptotic p, and an effect size. All of
it is written to summary.json, not just printed.

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
import math
import re
from pathlib import Path

import numpy as np
from scipy import stats

POC_DIR = Path(__file__).resolve().parent
REPO_ROOT = POC_DIR.parent

DEFAULT_CONFIG = REPO_ROOT / "experiments" / "exp002" / "conditions.draft.json"
DEFAULT_RESULTS = POC_DIR / "results" / "exp002" / "raw"

BOOTSTRAP_N = 10_000
BOOTSTRAP_SEED = 20260728  # fixed + recorded so the CIs are reproducible

# Paid-tier price per 1M tokens (USD). Source: price_token.md (Gemini API).
PRICE_PER_1M = {
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},
}
FALLBACK_MODEL = "gemini-3.1-flash-lite"

CONDITION_RE = re.compile(r"^(A\d)(B\d)$")


# --- small stats helpers ---------------------------------------------------

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value (binomial, p=0.5) on discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def cochrans_q(matrix: list[list[int]]) -> dict:
    """Cochran's Q for k paired binary treatments over N subjects.
    `matrix` is N rows x k cols of 0/1. Returns Q, df, and chi-square p."""
    a = np.asarray(matrix, dtype=float)
    n, k = a.shape
    col = a.sum(axis=0)          # per-condition correct counts
    row = a.sum(axis=1)          # per-clip correct-across-conditions counts
    denom = k * row.sum() - (row ** 2).sum()
    if denom == 0:               # every row all-0 or all-1: no discrimination
        return {"Q": 0.0, "df": k - 1, "p": 1.0, "n": int(n),
                "note": "no informative rows (all conditions agree on every clip)"}
    q = (k - 1) * (k * (col ** 2).sum() - col.sum() ** 2) / denom
    p = float(stats.chi2.sf(q, k - 1))
    return {"Q": round(float(q), 4), "df": k - 1, "p": round(p, 6), "n": int(n)}


def holm_bonferroni(pairs: list[dict], key: str = "p") -> None:
    """Add a Holm-Bonferroni corrected p-value (`p_holm`) in place."""
    order = sorted(range(len(pairs)), key=lambda i: pairs[i][key])
    m = len(pairs)
    running = 0.0
    for rank, idx in enumerate(order):
        adj = min(1.0, (m - rank) * pairs[idx][key])
        running = max(running, adj)  # enforce monotonicity
        pairs[idx]["p_holm"] = round(running, 6)


def bootstrap_ci(outcomes: list[float], n_boot: int, seed: int) -> list[float]:
    """Percentile 95% CI for the mean by resampling per-clip outcomes.
    Outcomes are per-clip correctness fractions in [0,1] (mean over repeats), so
    the CI is coherent with the reported accuracy_mean."""
    if not outcomes:
        return [0.0, 1.0]
    rng = np.random.default_rng(seed)
    arr = np.asarray(outcomes)
    means = arr[rng.integers(0, len(arr), size=(n_boot, len(arr)))].mean(axis=1)
    return [round(float(np.percentile(means, 2.5)), 4),
            round(float(np.percentile(means, 97.5)), 4)]


def wilcoxon_test(x: list[float], y: list[float]) -> dict:
    """Paired Wilcoxon signed-rank with matched-pairs rank-biserial effect size."""
    dx = np.asarray(x) - np.asarray(y)
    nz = dx[dx != 0]
    n = int(nz.size)
    if n == 0:
        return {"test": "Wilcoxon signed-rank", "n": 0, "W": None,
                "p": 1.0, "effect_size_rank_biserial": 0.0,
                "note": "all paired differences are zero"}
    ranks = stats.rankdata(np.abs(nz))
    t_plus = float(ranks[nz > 0].sum())
    t_minus = float(ranks[nz < 0].sum())
    r = (t_plus - t_minus) / (t_plus + t_minus)
    method = "exact" if n <= 25 else "approx"
    res = stats.wilcoxon(nz, alternative="two-sided", method=method,
                         zero_method="wilcox")
    return {"test": "Wilcoxon signed-rank", "n": n, "W": round(float(res.statistic), 2),
            "p": round(float(res.pvalue), 6),
            "effect_size_rank_biserial": round(float(r), 4), "method": method}


def cohens_g(b: int, c: int) -> float:
    """McNemar effect size: departure of the discordant split from 0.5."""
    n = b + c
    return round(abs(b / n - 0.5), 4) if n else 0.0


# --- data loading ----------------------------------------------------------

def load_config(path: Path) -> list[str]:
    cfg = json.loads(path.read_text())
    conditions = cfg.get("conditions")
    if not conditions:
        raise SystemExit(f"No 'conditions' in {path}")
    return conditions


def load_results(results_dir: Path) -> dict[tuple[str, str, int], dict]:
    """Key raw responses by (clip_id, condition, repeat)."""
    out: dict[tuple[str, str, int], dict] = {}
    for f in results_dir.glob("*.json"):
        if f.name.startswith("DRY__"):
            continue
        r = json.loads(f.read_text())
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


def verdict_correct(resp: dict, truth: str) -> bool:
    v = (resp.get("verdict") or "").strip().lower()
    return v == truth  # invalid/unparseable verdicts are simply != truth -> wrong


# --- main ------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    ap.add_argument("--manifest", type=Path, default=POC_DIR / "manifest.json")
    args = ap.parse_args()

    conditions = load_config(args.config)
    manifest = json.loads(args.manifest.read_text())
    cases = {c["id"]: c for c in manifest["cases"]}
    results = load_results(args.results_dir)
    if not results:
        raise SystemExit(f"No results in {args.results_dir}. Run run_trial.py first.")

    model = next(iter(results.values())).get("model", FALLBACK_MODEL)
    price, price_assumed = price_for(model)
    repeats_seen = sorted({k[2] for k in results})

    # correctness[cond][clip] = majority-vote correct (bool), used for the paired
    # binary tests (McNemar / Cochran). correctness_frac[cond][clip] = fraction of
    # repeats correct (in [0,1]), used for the accuracy bootstrap so its CI is
    # coherent with accuracy_mean rather than the collapsed majority vote. Also
    # collect per-clip continuous means (tokens/latency/cost) over repeats.
    correctness: dict[str, dict[str, bool]] = {c: {} for c in conditions}
    correctness_frac: dict[str, dict[str, float]] = {c: {} for c in conditions}
    continuous: dict[str, dict[str, dict[str, float]]] = {c: {} for c in conditions}
    per_cond: dict[str, dict] = {}

    for cond in conditions:
        is_b1 = cond.endswith("B1")
        # per-repeat accuracy (for mean +/- SD) and aggregates
        rep_acc: list[float] = []
        invalid = 0
        for rep in repeats_seen:
            got = correct = 0
            for cid, case in cases.items():
                r = results.get((cid, cond, rep))
                if r is None:
                    continue
                got += 1
                correct += verdict_correct(r, case["ground_truth"])
            if got:
                rep_acc.append(correct / got)

        # majority-vote per clip + continuous means
        fp = tn = tp = fn = 0
        by_type: dict[str, list[int]] = {}
        type_match = type_total = 0
        for cid, case in cases.items():
            reps = [results.get((cid, cond, rep)) for rep in repeats_seen]
            reps = [r for r in reps if r is not None]
            if not reps:
                continue
            oks = [verdict_correct(r, case["ground_truth"]) for r in reps]
            invalid += sum(
                1 for r in reps
                if (r.get("verdict") or "").strip().lower() not in ("normal", "anomaly")
            )
            ok = sum(oks) >= len(oks) / 2  # majority vote (>=half)
            correctness[cond][cid] = ok
            correctness_frac[cond][cid] = sum(oks) / len(oks)
            # continuous (per-clip mean over repeats)
            toks = [(_u := r.get("usage")) and _u.get("total_tokens") for r in reps]
            toks = [t for t in toks if t]
            lats = [r.get("latency_s") for r in reps if r.get("latency_s") is not None]
            costs = [call_cost(r.get("usage"), price) for r in reps]
            costs = [c for c in costs if c is not None]
            continuous[cond][cid] = {
                "total_tokens": float(np.mean(toks)) if toks else None,
                "latency_s": float(np.mean(lats)) if lats else None,
                "cost_usd": float(np.mean(costs)) if costs else None,
            }
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

        clip_fracs = list(correctness_frac[cond].values())
        acc_mean = float(np.mean(rep_acc)) if rep_acc else None
        acc_sd = float(np.std(rep_acc, ddof=1)) if len(rep_acc) > 1 else 0.0

        def cont_mean(metric: str) -> float | None:
            vals = [d[metric] for d in continuous[cond].values() if d[metric] is not None]
            return round(float(np.mean(vals)), 4) if vals else None

        per_cond[cond] = {
            "clips_scored": len(clip_fracs),
            "accuracy_mean": round(acc_mean, 4) if acc_mean is not None else None,
            "accuracy_sd": round(acc_sd, 4),
            "accuracy_ci95_bootstrap": bootstrap_ci(clip_fracs, BOOTSTRAP_N, BOOTSTRAP_SEED),
            "false_positive_rate": round(fp / (fp + tn), 4) if (fp + tn) else None,
            "anomaly_recall": round(tp / (tp + fn), 4) if (tp + fn) else None,
            "recall_by_bug_type": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_type.items())},
            "invalid_responses": invalid,
            "mean_total_tokens": cont_mean("total_tokens"),
            "mean_latency_s": cont_mean("latency_s"),
            "mean_cost_usd": cont_mean("cost_usd"),
            **({"defect_type_match": f"{type_match}/{type_total}"} if is_b1 and type_total else {}),
        }

    # --- omnibus: Cochran's Q over clips answered under EVERY condition ------
    common = sorted(set.intersection(*(set(correctness[c]) for c in conditions)))
    matrix = [[int(correctness[c][cid]) for c in conditions] for cid in common]
    omnibus = cochrans_q(matrix) if matrix and len(conditions) > 1 else None

    # --- post-hoc accuracy: pairwise exact McNemar + Holm -------------------
    posthoc_acc: list[dict] = []
    for a, b in itertools.combinations(conditions, 2):
        paired = sorted(set(correctness[a]) & set(correctness[b]))
        n_b = sum(1 for i in paired if correctness[a][i] and not correctness[b][i])
        n_c = sum(1 for i in paired if not correctness[a][i] and correctness[b][i])
        posthoc_acc.append({
            "pair": f"{a} vs {b}", "test": "exact McNemar",
            "n_paired": len(paired), "n_discordant": n_b + n_c,
            f"{a}_only_correct": n_b, f"{b}_only_correct": n_c,
            "p": round(mcnemar_exact(n_b, n_c), 6),
            "effect_size_cohens_g": cohens_g(n_b, n_c),
        })
    if posthoc_acc:
        holm_bonferroni(posthoc_acc)

    # --- post-hoc efficiency: pairwise Wilcoxon on tokens/latency/cost ------
    posthoc_cont: dict[str, list[dict]] = {}
    for metric in ("total_tokens", "latency_s", "cost_usd"):
        rows: list[dict] = []
        for a, b in itertools.combinations(conditions, 2):
            paired = sorted(set(continuous[a]) & set(continuous[b]))
            xa = [continuous[a][i][metric] for i in paired]
            xb = [continuous[b][i][metric] for i in paired]
            keep = [(u, v) for u, v in zip(xa, xb) if u is not None and v is not None]
            if not keep:
                continue
            xa, xb = [u for u, _ in keep], [v for _, v in keep]
            row = {"pair": f"{a} vs {b}", **wilcoxon_test(xa, xb),
                   f"{a}_median": round(float(np.median(xa)), 4),
                   f"{b}_median": round(float(np.median(xb)), 4)}
            rows.append(row)
        if rows:
            holm_bonferroni(rows)
        posthoc_cont[metric] = rows

    summary = {
        "experiment": "EXP002",
        "model": model,
        "price_per_1M_usd": price,
        "price_assumed": price_assumed,
        "conditions": conditions,
        "repeats_seen": repeats_seen,
        "bootstrap": {"n": BOOTSTRAP_N, "seed": BOOTSTRAP_SEED},
        "per_condition": per_cond,
        "omnibus_cochran_q": omnibus,
        "posthoc_accuracy_mcnemar_holm": posthoc_acc,
        "posthoc_efficiency_wilcoxon_holm": posthoc_cont,
    }
    out_path = args.results_dir.parent / "summary.json"
    out_path.write_text(json.dumps(summary, indent=2) + "\n")

    print(json.dumps(summary, indent=2))
    print(f"\nSummary written to {out_path}")


if __name__ == "__main__":
    main()
