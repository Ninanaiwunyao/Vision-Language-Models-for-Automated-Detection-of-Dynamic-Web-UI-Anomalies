"""The statistical tests EXP002 reports, and the power model that planned them.

Kept in one module so `score_trial.py` (which applies the tests to the results)
and `power_analysis.py` (which simulated their power beforehand) provably share
an implementation — a power model computed from a *different* McNemar formula
than the one finally applied would not be a pre-registration of anything.

Every function returns plain Python/JSON-serialisable values, and each test
reports its n and an effect size alongside the p-value.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import stats


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value (binomial, p=0.5) on discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def cohens_g(b: int, c: int) -> float:
    """McNemar effect size: departure of the discordant split from 0.5."""
    n = b + c
    return round(abs(b / n - 0.5), 4) if n else 0.0


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


def wilcoxon_signed_rank(x: list[float], y: list[float]) -> dict:
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
