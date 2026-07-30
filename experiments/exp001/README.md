# EXP001 — Main paired trial

Frozen on 2026-07-25 from `44356d7` on branch `exp/formal-trial-v1`.

[`config.frozen.json`](config.frozen.json) is the single source of truth for
this experiment. It is not documentation of what the code does — it is the
specification the code must be made to match before the run starts.

## What "frozen" means here

Every field in the config is fixed for the whole of EXP001. Changing any of
them mid-run means clips were judged under different conditions, which destroys
the paired (McNemar) comparison — the whole point of the design. A change after
the freeze requires a new `experiment_id` and a re-run of **all** clips under
**both** conditions.

The one deliberate exception is `pipeline/score_trial.py`: scoring is a pure
function of the cached raw responses in `results/exp001/raw/`, so it can be fixed
or extended afterwards without invalidating the run.

## Three code changes required before the run

The PoC pipeline works, but three of its settings are resolved at runtime
rather than pinned. Each is marked `_change_required` in the config.

1. **Temperature is never set.** `vlm_client.call_json()` calls
   `generate_content(parts)` with no `generation_config`, so the PoC ran at the
   SDK default — temperature 1.0, i.e. stochastic sampling. EXP001 specifies
   `temperature: 0.0` and must pass it explicitly on every call, and record it
   in each per-clip raw JSON.

2. **The model is auto-probed, not pinned.** `vlm_client.connect()` sorts
   available models `flash-lite` before `flash` and takes the first one with
   quota. That is a runtime decision, not a frozen one. With `GEMINI_MODEL`
   unset, EXP001 must abort rather than auto-select.

3. **Dataset generation is unseeded.** The Playwright spec drives todo text,
   item counts, toggles and filter walks from bare `Math.random()`. The config
   declares `rng_seed: 20260725`; until the spec consumes it through a seeded
   PRNG, `dataset_v3` is reproducible only as a stored artifact, never as a
   procedure someone else could re-run.

A fourth, smaller one: pin `generator/package.json` to an exact Playwright
`1.61.1` instead of `^1.61.1` before generating the dataset.

## Run order

```bash
# 0. apply the four changes above, then regenerate the dataset
cd generator && npx playwright test bug-injection.spec.ts   # CONTROL_CASES=60, CASES_PER_BUG=10
node scripts/collect-dataset.mjs                             # -> dataset_v3/

# 1. video-level gate (a clip that lost its defect frame is not ground truth)
python3 pipeline/gate_videos.py

# 2. manifest over all 120 clips (no subsetting for EXP001)
python3 pipeline/build_manifest.py dataset_v3

# 3. pipeline smoke test — must exit 0
export GEMINI_MODEL=gemini-3.1-flash-lite
export GOOGLE_API_KEY=...        # never committed
python3 pipeline/smoke_test.py

# 4. dry run: build every payload, zero API calls — inspect prompts first
python3 pipeline/run_trial.py --dry-run

# 5. the real run (240 calls: 120 clips x 2 conditions, interleaved, resumable)
python3 pipeline/run_trial.py
python3 pipeline/score_trial.py
```

## Design summary

|  | C1_BASELINE | C2_OURS |
|---|---|---|
| frame selection | uniform over clip | strongest sustained transition + final frame |
| images | 6 | 3 |
| prompt | `baseline/v1` | `ours/v1` |
| DOM signal | no | yes |

120 clips, 60 normal / 60 anomaly, 10 per bug type across all six types.
Subject: TodoMVC React only — the condition is the only manipulated variable.

Prompt templates are pinned by SHA-256 in the config; verify them before the
run, because a drifted prompt is invisible in the results.
