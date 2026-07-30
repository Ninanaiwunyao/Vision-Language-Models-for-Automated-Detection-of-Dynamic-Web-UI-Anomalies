#!/usr/bin/env python3
"""EXP002 pre-registration — simulation-based power for the paired McNemar test.

The paired comparison between two conditions is an exact McNemar test on the
DISCORDANT pairs only (clips where exactly one condition is correct). Its power
therefore depends on:

  n       number of paired clips evaluated,
  p_disc  probability that a given clip is a discordant pair,
  psi     among discordant pairs, the probability the pair favours the *better*
          condition (psi = 0.5 is the null; psi = 1.0 means every disagreement
          favours the treatment).

Marginal accuracy (e.g. 0.75 vs 0.94) does NOT by itself determine McNemar power
— two runs with the same accuracy gap can have very different discordant-pair
counts. So we parameterise directly on (n, p_disc, psi).

Design decision recorded here: dataset_v2 is already fixed, so n is not a free
knob we solve for. We instead report, at the fixed operating n, the *minimum
detectable effect* — which (p_disc, psi) cells reach power >= 0.80. If the
planning cell falls short, that is the trigger to decide whether to generate
more videos, not a silent switch to a bigger n.

Everything is seeded (SEED below) and written to results/power_analysis.json so
the grid is reproducible and citable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

PIPELINE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPELINE_DIR))

import paths  # noqa: E402
from stats_tests import mcnemar_exact  # noqa: E402

OUT = paths.RESULTS_DIR / "power_analysis.json"

# --- Frozen analysis parameters (recorded, not defaults to be overridden) -----
SEED = 20260727           # fixed so the grid is bit-reproducible
N_SIM = 100_000           # Monte-Carlo trials per grid cell
# Operating number of paired clips for EXP002. Resolved to the full dataset_v3
# (120 clips): at the planning cell the n=40 exploratory grid was underpowered
# (0.38), so EXP002 evaluates all 120 paired clips (planning power 0.94). n=40 is
# retained below only to document that decision.
N_OPERATING = 120
N_EXPLORATORY = 40
N_GRID = (N_EXPLORATORY, N_OPERATING)
ALPHA = 0.05

P_DISC_GRID = (0.15, 0.20, 0.25, 0.30, 0.40)
PSI_GRID = (0.75, 0.85, 0.90, 1.00)

# Headline planning cell. psi=1.00 (the pilot's b=3,c=0) is winner's-curse
# inflated at n=16 and is NOT a valid planning basis; we plan on a deliberately
# conservative disagreement split instead.
PLAN_P_DISC = 0.20
PLAN_PSI = 0.85
TARGET_POWER = 0.80


# Cache the reject/accept decision for every (b, c) with b + c <= n so the
# Monte-Carlo loop never recomputes a p-value. Index by (n_disc, c).
# The p-value comes from stats_tests.mcnemar_exact — the *same* function
# score_trial.py applies to the real results, so this is a power model of the
# test that will actually be run, not of a lookalike.
def _reject_table(n_max: int, alpha: float) -> dict[tuple[int, int], bool]:
    table: dict[tuple[int, int], bool] = {}
    for nd in range(n_max + 1):
        for c in range(nd + 1):
            table[(nd, c)] = mcnemar_exact(nd - c, c) < alpha
    return table


def simulate_power(
    n: int, p_disc: float, psi: float, alpha: float, n_sim: int,
    rng: np.random.Generator, reject: dict[tuple[int, int], bool],
) -> float:
    """Monte-Carlo power at one grid cell.

    Each simulated trial: draw the discordant count n_disc ~ Binomial(n, p_disc),
    then the treatment-favouring discordants c ~ Binomial(n_disc, psi); the test
    rejects iff the two-sided exact McNemar p-value < alpha."""
    n_disc = rng.binomial(n, p_disc, size=n_sim)
    c = rng.binomial(n_disc, psi)  # elementwise: c[i] ~ Binomial(n_disc[i], psi)
    hits = 0
    for nd_i, c_i in zip(n_disc.tolist(), c.tolist()):
        if reject[(nd_i, c_i)]:
            hits += 1
    return hits / n_sim


def build_grid(
    n: int, rng: np.random.Generator, reject: dict[tuple[int, int], bool],
) -> list[dict]:
    grid: list[dict] = []
    for psi in PSI_GRID:  # rows = psi, cols = p_disc
        for p_disc in P_DISC_GRID:
            power = simulate_power(n, p_disc, psi, ALPHA, N_SIM, rng, reject)
            grid.append({
                "p_disc": p_disc,
                "psi": psi,
                "n": n,
                "power": round(power, 4),
                "expected_discordant": round(n * p_disc, 1),
                "meets_target": power >= TARGET_POWER,
            })
    return grid


def print_grid(n: int, grid: list[dict], is_operating: bool) -> None:
    tag = "OPERATING" if is_operating else "exploratory"
    headline = next(
        g for g in grid if g["p_disc"] == PLAN_P_DISC and g["psi"] == PLAN_PSI
    )
    print(f"\n[{tag}]  n={n}  alpha={ALPHA}  (cells reaching "
          f"power>={TARGET_POWER:.2f} marked *)")
    header = "psi \\ p_disc |" + "".join(f"  {p:>5.2f}" for p in P_DISC_GRID)
    print(header)
    print("-" * len(header))
    for psi in PSI_GRID:
        row = f"   {psi:>4.2f}     |"
        for p_disc in P_DISC_GRID:
            g = next(x for x in grid if x["psi"] == psi and x["p_disc"] == p_disc)
            row += f" {g['power']:>5.2f}{'*' if g['meets_target'] else ' '}"
        print(row)
    print(f"  headline (p_disc={PLAN_P_DISC}, psi={PLAN_PSI}): "
          f"power={headline['power']:.3f} "
          f"({'MEETS' if headline['power'] >= TARGET_POWER else 'BELOW'} "
          f"{TARGET_POWER:.2f})")


def main() -> None:
    rng = np.random.default_rng(SEED)
    reject = _reject_table(max(N_GRID), ALPHA)

    grids = {n: build_grid(n, rng, reject) for n in N_GRID}

    def headline_of(n: int) -> float:
        return next(
            g["power"] for g in grids[n]
            if g["p_disc"] == PLAN_P_DISC and g["psi"] == PLAN_PSI
        )

    result = {
        "test": "paired two-sided exact McNemar",
        "method": "Monte-Carlo simulation",
        "seed": SEED,
        "n_sim_per_cell": N_SIM,
        "alpha": ALPHA,
        "target_power": TARGET_POWER,
        "framing": (
            "minimum detectable effect at fixed n; dataset_v3 fixed at 120 clips, "
            "so n is not solved for"
        ),
        "n_operating": N_OPERATING,
        "n_exploratory": N_EXPLORATORY,
        "planning_cell": {"p_disc": PLAN_P_DISC, "psi": PLAN_PSI},
        "planning_power": {str(n): round(headline_of(n), 4) for n in N_GRID},
        "decision": (
            f"n={N_EXPLORATORY} was underpowered at the planning cell "
            f"({headline_of(N_EXPLORATORY):.2f}); EXP002 evaluates all "
            f"{N_OPERATING} paired clips (planning power "
            f"{headline_of(N_OPERATING):.2f}), no new videos generated."
        ),
        "planning_note": (
            "psi=1.00 (pilot b=3,c=0 at n=16) is winner's-curse inflated and is "
            "not a valid planning basis; headline uses psi=0.85, p_disc=0.20."
        ),
        "grids": {str(n): grids[n] for n in N_GRID},
    }
    paths.write_json(OUT, result)

    print(f"Paired exact-McNemar power  |  seed={SEED}  sims/cell={N_SIM:,}")
    for n in N_GRID:
        print_grid(n, grids[n], is_operating=(n == N_OPERATING))
    print(f"\nWritten: {OUT.relative_to(paths.REPO_ROOT)}")


if __name__ == "__main__":
    main()
