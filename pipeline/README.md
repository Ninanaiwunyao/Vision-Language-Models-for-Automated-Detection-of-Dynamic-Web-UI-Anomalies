# Evaluation pipeline (`pipeline/`)

The apparatus behind **EXP002**, the formal factorial ablation of the dual-path
VLM defect detector. Stages 1 and 2 are gates that must both be green **before**
running the trial: if they are not, an accuracy number is unattributable — you
can't tell "the method doesn't work" from "the pipeline is broken".

EXP002 is specified by `experiments/exp002/config.frozen.json`. That file, not
this README, is authoritative for the formal run; everything below describes how
the scripts execute it. (EXP001 — the n=16 two-condition pre-trial this grew
from — stays immutable under `experiments/exp001/`.)

## Stage 1 — Ground-truth manifest

```bash
python3 pipeline/build_manifest.py                 # newest dataset, default mode
```

Reads a dataset's `metadata.json` (produced by the Playwright fault-injection
matrix) and emits:

- `data/manifest.json` — rich, machine-readable ground truth (one object/video)
- `data/manifest.csv` — flat view for quick inspection

Two selection modes:

| mode | contents | default for |
|---|---|---|
| `full` | every clip in the dataset, no subsetting | `dataset_v3` and later |
| `pretrial` | the balanced 16-clip subset (8 normal / 8 anomalous, `blur` and `skew` sampled twice) | `dataset_v1`, `dataset_v2` |

EXP002 runs `full` over **dataset_v3: 120 clips, 60 normal / 60 anomalous, 10
per bug type** — the whole set is evaluated because the power analysis (below)
found a 40-clip subset underpowered. `pretrial` is retained only to rebuild the
older EXP001 subset; in that mode `N_CONTROLS` / `BUG_SELECTION` in
`build_manifest.py` control the sampling, and in `full` mode they are bypassed.

```bash
python3 pipeline/build_manifest.py dataset_v3      # -> full (120 clips)
python3 pipeline/build_manifest.py dataset_v2 --pretrial
```

Every label is **derived, not hand-written**: because each bug is a known CSS
mutation injected by `generator/tests/bug-injection.spec.ts`, the
`ground_truth` (`normal`/`anomaly`) and the `expectation` symptom text come
straight from the bug key.

### Manifest fields

| field | meaning |
|-------|---------|
| `id` | dataset case id |
| `ground_truth` | `normal` or `anomaly` — the binary detection label |
| `is_anomaly` | boolean form of the above |
| `bug_type` | injected defect key (null for normal) |
| `bug_label` | human-readable defect name from the bug matrix |
| `expectation` | expected symptom, auto-derived from the bug type — used for semantic grading |
| `video_path` | repo-relative path to `video.webm` |
| `dom_trace_path` | repo-relative path to `domtrace.json`, or `null` when the dataset predates it — the DOM signal behind A3 selection and the B1 prompt |
| `keyframes` | `normal` / `buggy` / `diff` PNG paths |
| `diff_ratio` | measured pixel-diff ratio (severity evidence) |
| `min_diff_ratio` | the quality-gate threshold this clip cleared |
| `target_url` | the subject application under test |
| `browser` | recording browser (`chromium`) |

## Stage 2 — Four-stage smoke test

```bash
export GOOGLE_API_KEY=...            # or GEMINI_API_KEY
python3 pipeline/smoke_test.py            # exit 0 = gate GREEN
```

Runs four checks in order, short-circuiting on the first failure:

1. **NETWORK** — reach `generativelanguage.googleapis.com` (DNS + TLS + HTTP).
2. **AUTH** — API key present and accepted; lists the account's models.
3. **IMAGE** — load a manifest keyframe, base64-encode it (with a reversibility
   check), and confirm the multimodal model actually receives and describes it.
4. **JSON** — make the model return strict JSON and parse it.

Exit code `0` means it is safe to start the PoC; any non-zero exit names the
stage that broke.

### Model selection & quota

Unless you pin one with `GEMINI_MODEL=...`, checks 3-4 above try a prioritised
list of `*-flash` models and **fall back to the next on a `429` quota error**
(`gemini-2.5-flash` first, `gemini-2.0-flash` last). This matters because the
free tier can be zeroed out (`limit: 0`) for a *specific* model while others on
the same key still work.

If **every** candidate returns 429, the key/project has no free-tier quota left:
enable billing on the Google Cloud project, wait for the daily reset, or set
`GEMINI_MODEL` to a model you have quota for.

That fallback is a convenience for the *smoke test only*. The trial itself must
run on one frozen judge, so EXP002 pins `GEMINI_MODEL=gemini-3.1-flash-lite`;
there a 429 retries the same model with backoff and never switches.

## Pre-registration — power analysis

```bash
python3 pipeline/power_analysis.py        # -> results/power_analysis.json
```

Run **before** the trial, so the analysis plan is fixed independently of the
result. The paired comparison is an exact McNemar test on discordant pairs only,
so power depends on `n`, the discordant-pair rate `p_disc`, and `psi` (the share
of disagreements favouring the better condition) — not on the marginal accuracy
gap. The clip count is fixed by the dataset, so the script reports the *minimum
detectable effect* at that `n` rather than solving for a sample size.

At n=120 the planning cell (`p_disc=0.20`, `psi=0.85`) reaches power **0.94**;
the n=40 exploratory grid reached only 0.38, which is why all 120 clips are
evaluated. The pilot's `psi=1.00` is winner's-curse inflated and is deliberately
not used as a planning basis. Everything is seeded (`SEED = 20260727`) so the
grid is reproducible.

## Stage 3 — Factorial ablation trial (the core)

```bash
python3 pipeline/run_trial.py --dry-run   # build every payload, zero API calls
python3 pipeline/run_trial.py             # the real thing (needs GOOGLE_API_KEY)
python3 pipeline/score_trial.py           # metrics + omnibus + post-hoc tests
```

EXP002 is a **2-factor factorial ablation, paired within clip**. Factor **A** is
which frames are sent; factor **B** is the prompt:

| factor | level | meaning |
|---|---|---|
| A — frame selection | `A0` | uniform sampling, 6 frames (no selection) |
| | `A1` | pixel-diff-only keyframes, 3 frames (strongest sustained transition + before/after/final) |
| | `A3` | pixel + DOM **fused** keyframes, 3 frames (pixel score down-weighted by nearby DOM activity) |
| B — prompt | `B0` | naive prompt |
| | `B1` | dual-path prompt (generic expected-appearance reference + structural signals + DOM activity summary) |

Four of the six cells are run, chosen so each factor is isolated:

| condition | frames | prompt | role |
|---|---|---|---|
| `A0B0` | uniform, 6 | naive | brute-force baseline |
| `A1B1` | pixel-only, 3 | dual-path | anchors the EXP001 pilot's "ours" |
| `A3B0` | fused, 3 | naive | isolates the **selection** contribution (vs `A0B0`) |
| `A3B1` | fused, 3 | dual-path | the full proposed method; vs `A3B0` isolates the **prompt** contribution |

`A2` (DOM-only selection) is dropped from the headline set — it would need a
selector that has not been built.

A3's fusion rule is `fused_score(i) = pixel_score(i) / (1 + λ · dom_mutations_near(i))`
with `λ = 1.0` over a 1200 ms window. The reasoning: a CSS-only rendering defect
changes pixels but produces **no** DOM mutation (the injected stylesheet sits in
`<head>`, unobserved), whereas legitimate user actions do mutate the DOM — so A3
prefers strong pixel transitions *not* accompanied by DOM activity. With no
trace available it degenerates to A1.

Every clip runs under **every** condition, **3 times** (120 × 4 × 3 = **1440
calls**). Temperature is 0.0, so the repeats are not probing sampling spread —
they capture residual API non-determinism and give real variance for latency,
tokens and cost (SD is reported even when it is 0.00). Binary outcomes are
collapsed across repeats by **majority vote** before the paired tests.

Fairness rules (enforced in code):
- One model, pinned and verified against the account's live model list, frozen
  for all calls; a 429 retries the same model with backoff and never switches —
  if the model is unavailable the run aborts.
- The manifest's per-clip `expectation` never enters any prompt; the expected-
  appearance text under B1 is generic and byte-identical for every clip.
- Keyframe selection reads only pixels and the DOM trace, never the label.
- Conditions *and* repeats are interleaved per clip, and every response is
  cached in `results/exp002/raw/` — a quota crash mid-run costs every arm
  the same clips, so pairing holds and a re-run resumes.

### Scoring

`score_trial.py` is config-driven and reads only that cache. Per condition it
reports accuracy (mean ± SD across repeats plus a bootstrap 95% CI, 10 000
resamples, seed 20260728), false-positive rate on the 60 normals, recall on the
60 anomalies overall and per bug type, defect-type match for the B1 conditions,
unparseable responses (counted as **wrong**), and mean tokens / latency / cost.

Across conditions it runs an omnibus **Cochran's Q** over the per-clip
correctness matrix, then post-hoc pairwise **exact McNemar** (Holm-Bonferroni,
effect size Cohen's g) for accuracy and pairwise **Wilcoxon signed-rank**
(Holm-Bonferroni, matched-pairs rank-biserial) for tokens / latency / cost.
Everything is written to `summary.json`, not just printed.

## Frozen configuration & freeze policy

The condition set, repeats, generation config, keyframe parameters, model pin
and cost model all live in `experiments/exp002/config.frozen.json`, which both
`run_trial.py` and `score_trial.py` load — adding or removing a condition is a
config edit, not a code change. The three prompt templates in `run_trial.py` are
integrity-pinned there by SHA-256; a mismatch means a prompt drifted and the run
is no longer the one that was specified.

Once a config is marked `frozen`, **no field may change while results are being
collected.** Any change requires a new `experiment_id` and a fresh run of all
clips under all conditions — a partial re-run breaks the paired design. The
ablation is only interpretable if every cell saw the same judge, the same
decoding and the same prompts, with the manipulated factor as the sole
difference.

This is why EXP002 is a new id rather than an edit of EXP001: the condition set
changed from 2 endpoints to a 4-cell factorial, so the policy required a new
experiment. `experiments/exp001/` stays immutable — its n=16 numbers were
already reported, and its two arms map onto EXP002's `A0B0` and `A1B1`.

The one deliberate exception: `score_trial.py` is **not** frozen. Scoring is a
pure function of the cached raw responses, so it may be corrected or extended
after the run without invalidating it.

## Module layout

Entry-point scripts, each runnable as `python3 pipeline/<name>.py`:

| script | stage |
|---|---|
| `build_manifest.py` | 1 — ground-truth manifest from a dataset's `metadata.json` |
| `gate_videos.py` | dataset quality gate (defect must be visible *on video*) |
| `smoke_test.py` | 2 — four-stage pipeline gate (network / auth / image / JSON) |
| `power_analysis.py` | pre-registration: simulated power for the paired McNemar test |
| `run_trial.py` | 3 — runs every clip under every condition, caches each response |
| `score_trial.py` | 3 — scores the cached responses into `summary.json` |

Shared modules (imported, never run directly):

| module | responsibility |
|---|---|
| `paths.py` | repo-relative paths + JSON read/write, resolved from the file, not the CWD |
| `conditions.py` | the `AxBy` condition grammar and the frozen-config loader |
| `keyframes.py` | video decoding and the factor-A frame selectors (A0 / A1 / A3) |
| `stats_tests.py` | the tests EXP002 reports, shared with the power analysis |
| `vlm_client.py` | Gemini auth, the frozen-model rule, and JSON call/retry |

`stats_tests.py` is deliberately shared between `score_trial.py` and
`power_analysis.py`: a power model built on a *different* McNemar
implementation than the one finally applied would not pre-register anything.

### What the scripts read and write

Nothing here writes inside `pipeline/`. Code, dataset-derived ground truth and
experimental evidence live in separate top-level directories because they have
different lifecycles — code is refactorable, ground truth is regenerable from a
dataset, and results are evidence:

| directory | contents | lifecycle |
|---|---|---|
| `pipeline/` | these scripts | refactorable at any time |
| `data/` | `manifest.json` / `manifest.csv` | regenerate with `build_manifest.py` |
| `results/<exp>/` | `raw/` response cache, `summary.json`, `trial-config.json` | append-only evidence, in git |
| `experiments/<exp>/` | `config.frozen.json` | frozen spec, amendments recorded in-file |
| `dataset_v3/` | the .webm / .png / DOM-trace binaries | **not** in git; regenerable from the pinned generator |

`paths.py` is the single place these locations are defined; no other module
hard-codes one.

## Dataset integrity: why dataset_v2 exists

dataset_v1's *videos* frequently did not contain the labelled defect: Playwright
only receives a frame when the page repaints, and its recorder finalises
asynchronously on close — so a static broken state (vanished app, off-screen
app) emitted a single defect frame that often lost the close race. Every
`flyaway_offset` clip and 3/5 `vanish_invisible` clips were corrupted this way,
while animated bugs (`flicker`) always survived. The screenshot-level pixelmatch
gate could not see this because it never decoded the video.

Three fixes in `generator/tests/bug-injection.spec.ts` + `pipeline/gate_videos.py`:
1. a 7s fixed post-injection hold, identical for both groups (so clip duration
   cannot leak the label);
2. a "screencast heartbeat" — a tiny grey dot outside the app region toggling
   4x/s — keeping frames flowing through static broken states;
3. a **video-level gate**: the final decoded frame must resemble `buggy.png`,
   not `normal.png`, or the case is rejected (`video-gate-report.json`).

dataset_v2 also records a MutationObserver trace (`domtrace.json`) per clip —
the DOM half of the dual-path signal. The post-injection typing was switched to
neutral phrases after we found the original typed the scenario id (including
the bug name!) into the on-screen input — a direct label leak to any model that
reads the screen.

dataset_v3 — the set EXP002 evaluates — inherits both gates and clears them:
the per-type screenshot floors, and the video gate at 120/120
(`dataset_v3/video-gate-report.json`). See `docs/dataset-standard.md`.

### Requirements

Python 3 with `google-generativeai`, `scipy`, `numpy`, `pillow`, `requests`,
and `imageio-ffmpeg` (all present in this environment). `scipy` backs the
Wilcoxon test and Cochran's Q's chi-square p-value; the exact pinned versions
EXP002 ran under are recorded in `environment_lock` in the frozen config. You
must supply your own Gemini API key via the environment — for security it is
never stored in the repo.
