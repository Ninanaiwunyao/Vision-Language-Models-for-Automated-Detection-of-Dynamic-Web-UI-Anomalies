# MSc Project Worklog

## 2026-07-11

### Completed
- **Dataset Collection (dataset_v1/)**: 
  - Re-ran Playwright test suite (40 tests, all passed in 1.7m)
  - All 40 cases passed pixelmatch quality gate (gate-report.json exists)
  - Collected into dataset_v1/ using collect-dataset.mjs
  - Structure: 10 normal + 30 buggy cases, each with video.webm + 3x PNG
  - Total size: 15MB
  - Cleaned up redundant test-results/
  - **Note**: dataset_v1/ NOT in .gitignore yet (consider adding if not tracking in git)

### Next Steps
- [ ] Add dataset_v1/ to .gitignore (optional, depends on versioning strategy)
- [ ] Prepare dataset for model training / evaluation pipeline

## 2026-07-16

### PoC pre-trial scaffolding (branch: feat/poc-pretrial)

- **Stage 1 — ground-truth manifest** (`poc/build_manifest.py`):
  - Generates `poc/manifest.json` + `poc/manifest.csv` from `dataset_v1/metadata.json`.
  - Balanced pre-trial subset: **16 videos, 8 normal / 8 anomalous**, all six bug
    types covered (blur & skew sampled x2 as the hardest, lowest-diff cases).
  - Labels/expectations are auto-derived from the injected bug key (not hand-written).
  - Guards enforce the requirements (10-20 videos, ~50/50 split, asset existence).
- **Stage 2 — four-stage smoke test** (`poc/smoke_test.py`): NETWORK -> AUTH ->
  IMAGE (base64 + multimodal round-trip) -> JSON parse. Short-circuits on first fail.
  - Added model fallback: auto-picks a `*-flash` model and falls back on a 429
    (quota) error, since `gemini-2.0-flash` returned free-tier `limit: 0` on this key.
  - **Gate is GREEN** — all four stages pass. Working model: **`gemini-3.5-flash`**.
    - Stage 1 NETWORK: host reachable (HTTP 404).
    - Stage 2 AUTH: key accepted, 39 models available.
    - Stage 3 IMAGE: model received & described the `bug_skew_distortion_1` frame.
    - Stage 4 JSON: strict-JSON response parsed (verdict field present).
  - Pipeline verified — safe to start the PoC evaluation.

### Next Steps
- [x] Build the PoC evaluation runner (superseded by the Stage 3 paired trial below).

## 2026-07-17

### Dataset integrity incident: dataset_v1 videos did not contain their defects

While preparing Stage 3, frame-level inspection revealed that many dataset_v1
*videos* never show the labelled bug: **0/5 flyaway_offset and 3/5
vanish_invisible clips end on a perfectly normal-looking app** (verified by
frame montage + a dark-pixel "ink" metric). Root cause: Playwright's recorder
only receives frames on repaint and finalises asynchronously on close; a static
broken state (vanished/off-screen app) emits one defect frame that often loses
the close race. Animated bugs (flicker) always survived — the tell.
The pixelmatch gate missed all of this because it compared screenshots and
never decoded the video.

Fixes (branch feat/poc-pretrial):
- Injection spec: 7s fixed post-injection hold (identical for both groups so
  duration cannot leak the label) + a "screencast heartbeat" dot outside the
  app clip region forcing ~4fps through static broken states.
- New **video-level quality gate** (`poc/gate_videos.py`): last decoded frame
  must resemble buggy.png, not normal.png. dataset_v1 would have failed it;
  dataset_v2 passes 40/40 (30/30 buggy defects provably on video).
- MutationObserver DOM traces (`domtrace.json`) recorded per clip — the DOM
  half of the dual-path signal. Injected CSS lives in <head> and is not
  observed, so the trace cannot leak the injection.
- **Label-leak fix**: post-injection typing used to type the scenario id
  (including the bug name!) into the on-screen input. Now types neutral todo
  phrases.
- dataset_v2 regenerated (40 cases), manifest rebuilt against it.

### Stage 3 pipeline built (paired Baseline-vs-Ours trial)

- `poc/keyframes.py` — decode + uniform sampling (baseline) and sustained-
  transition change-point selection + final settled frame (ours). Pixel-only
  probe confirmed the classes are NOT separable by change magnitude (normal
  typing produces bigger diffs than several bugs), so the structural signal
  does not leak the answer.
- `poc/run_trial.py` — both conditions per clip, interleaved, cached in
  poc/results/raw/ (quota-crash resumable); cheapest model frozen for the whole
  trial; per-clip ground truth never enters any prompt. `--dry-run` builds all
  payloads without API calls.
- `poc/score_trial.py` — accuracy + Wilson CI, FPR on normals, per-bug recall,
  exact McNemar on discordant pairs.

### Stage 3 trial results (model: gemini-3.1-flash-lite, 16 clips x 2 conditions)

> **Condition relabelling (2026-07-27, EXP002 factor scheme).** The pilot's two
> arms are relabelled below to the EXP002 code space. Under the frozen EXP002
> definition, **factor A is "which frames are sent"**: A1 = pixel-diff-only
> keyframe selection, A3 = pixel+DOM *fused* selection (DOM influences frame
> choice). The pilot's "ours" selector was **pixel-only** — DOM entered only the
> prompt, never the selection — so it was **A1B1, not A3B1**. "baseline" was
> uniform sampling + naive prompt = **A0B0**. These are NOT retroactively renamed
> to A3B1; A3 is a genuinely new selector implemented for EXP002.

| metric | A0B0 (uniform x6 + naive) | A1B1 (pixel-only keyframes x3 + dual-path) |
|---|---|---|
| accuracy | 0.75 [CI 0.51-0.90] | **0.94** [CI 0.72-0.99] |
| false-positive rate (8 normal) | 0.125 | **0.0** |
| anomaly recall (8 buggy) | 0.625 | **0.875** |
| defect-type match | — | 5/7 |

Paired: A1B1-only-correct **3**, A0B0-only-correct **0** (strict dominance;
McNemar exact p=0.25 — direction fully consistent, n=16 too small for
significance, as expected for a pre-trial). Model selection needed a quota
probe at connect time: this key's gemini-2.0 family has `limit: 0` and
gemini-2.5-flash-lite 404s for new accounts; first servable candidate was
gemini-3.1-flash-lite.

Failure analysis:
- `bug_displaynone_collapse_1` missed by BOTH conditions: a collapsed todo card
  (input + footer, list hidden) looks like a legitimately empty list; nearby
  typing mutations made `ours` read it as a user edit. The one genuinely hard
  case — keep for the full study.
- `control_normal_5` false-positived by baseline only ("duplicate entries"
  hallucinated from context-free uniform frames); `ours` correctly used the
  transition pair + DOM evidence. This is the FP-suppression story in one clip.
- `flyaway_offset` + one blur misclassified as `vanish_invisible` in the type
  field — semantically adjacent (app gone from view), verdicts still correct.

### Next Steps
- [x] Scale beyond the 16-clip pre-trial (full 40, more replicates per bug
      type) so McNemar has power — done as dataset_v3 below (120 clips).
- [ ] Consider a second model for generality.
- [ ] Investigate displaynone_collapse: both conditions blind to "section
      legitimately-looking removed" — candidate for DOM-signal refinement
      (removal without user input event nearby).

## 2026-07-25

### dataset_v3 generated at formal-trial scale (120 clips)

Retro-logged on 2026-07-27 — the dataset was produced on 07-25 but never
recorded here at the time; the freeze commit that pins its generator is
`e26134e` (2026-07-27).

Generated by the scaled fault-injection spec
(`latest/tests/poc-bug-injection.spec.ts`, CONTROL_CASES 60, CASES_PER_BUG 10)
against TodoMVC React, chromium, 1280x720, 7s fixed post-injection hold for
both groups.

| property | value |
|---|---|
| total clips | **120** (target was 100+) |
| normal : anomalous | **60 : 60** |
| bug types | 6, **10 clips each** (fully balanced) |
| generation window | 2026-07-25T16:17:17Z -> 16:23:10Z |
| on-disk size | 84MB, 603 files |

Per-case assets: `video.webm` + `normal.png` / `buggy.png` / `diff.png` +
`domtrace.json`. Manifest emitted as both `metadata.json` (120 cases) and
`metadata.csv`.

**Reproducibility.** Every case records `rngSeed` (20260725, identical across
all 120) and `rngScenarioSeed` (120 distinct values, derived per scenario via
RNG_SEED XOR FNV-1a(scenario.id)). The generator no longer calls
`Math.random()` anywhere, so the dataset is regenerable from the seed rather
than being a one-off artifact — which is what makes it eligible to back a
frozen experiment (`experiments/exp001/config.frozen.json`).

**Video-level quality gate — 120/120 PASS, 0 failures**
(`dataset_v3/video-gate-report.json`, produced by `poc/gate_videos.py`).
All 60 buggy clips returned "last frame resembles the defective state"
(last-frame diff vs normal 0.1091-0.6927, vs buggy ~0); the 60 controls
returned "control: no defect to verify". This is the same gate that dataset_v1
would have failed (see the 07-17 incident), so the defects are provably
*on video*, not merely in the screenshots.

Screenshot diff ratios by bug type, against the per-type `minDiffRatio`
thresholds (0.05-0.15) — all cleared:

| bug type | diff ratio range |
|---|---|
| flyaway_offset | 0.6305 - 0.6918 |
| vanish_invisible | 0.6002 - 0.6769 |
| flicker_seizure | 0.4021 - 0.5449 |
| displaynone_collapse | 0.4149 - 0.5442 |
| skew_distortion | 0.2778 - 0.2971 |
| blur_unreadable | 0.0750 - 0.1046 |

`blur_unreadable` remains by far the lowest-contrast class, consistent with the
pre-trial's read that blur and skew are the hardest cases — worth watching in
the per-bug recall breakdown of the formal trial.

**Not tracked in git by design.** `dataset_v3/` is in `.gitignore`: GitLab
project storage is capped at ~100MB and this dataset alone is 84MB. It must be
regenerated locally or on vLab from the pinned generator plus the frozen
RNG_SEED, not distributed through the repo. A clean `git status` therefore does
*not* mean the dataset is missing.

### Next Steps
- [ ] Run the EXP001 paired trial (C1_BASELINE vs C2_OURS) over all 120 clips;
      n=120 should give McNemar real power, unlike the n=16 pre-trial.
- [x] Rebuild the manifest against dataset_v3 — done 2026-07-27 (below).

## 2026-07-27

### build_manifest.py scaled to the full dataset_v3 manifest

The manifest builder was still a pre-trial tool: it only knew v1/v2, hard-coded
an 8+8 = 16-clip subset, and guarded `10 <= n <= 20`, so it could not produce
the formal-trial manifest EXP001 requires (`evaluation_subset: none` — all 120
clips, subsetting bypassed). `poc/manifest.json` still pointed at dataset_v2.

Changes to `poc/build_manifest.py`:
- **New `full` selection mode**: emits every clip, no subsetting. Controls
  first, then anomalies grouped in the injection matrix's order. Guards the
  properties the study needs (non-empty, all six bug types present, not wildly
  imbalanced) but drops the 10-20 size cap.
- **Mode auto-resolves per dataset**: `full` for dataset_v3 and later,
  `pretrial` for v1/v2 (kept for reproducibility). Override with
  `--full` / `--pretrial`.
- **Dataset discovery** now lists dataset_v3 first (newest-present default).
- **Renamed** `select_subset` -> `select_pretrial`, added `select_full` and a
  `select_cases` dispatcher; the old 16-clip path is unchanged.
- Manifest doc gains a `selection_mode` field (additive; schema_version stays
  2, so `run_trial.py` / `score_trial.py` readers are unaffected).

**Labels are still auto-derived from the bug key, never hand-written.** The
`EXPECTED_SYMPTOM` mapping (keyed on `bugKey`) is untouched, and `to_entry`
still resolves each clip's expectation through it. Added a guard that aborts if
a dataset contains a bug key with no expected-symptom mapping, rather than
silently dropping it.

Regenerated: `poc/manifest.json` + `poc/manifest.csv` now describe the full
dataset_v3 (120 clips, 60/60, 10 per type). Verified: every bug type maps to
exactly one distinct expectation string (label auto-derivation intact), all
downstream-required fields present on all 120 cases, zero
`ground_truth`/`is_anomaly` mismatches, and pretrial mode still produces the
original 16-clip subset for v2.

Command:

    python3 poc/build_manifest.py dataset_v3

### EXP002 planning — factor scheme + pre-registered power analysis

Designing EXP002 as a **factorial ablation** of the pipeline, superseding the
2-endpoint pilot. Factor definitions (frozen for EXP002):

- **Factor A = which frames are sent to the model.**
  - A0 = uniform sampling (no selection)
  - A1 = pixel-diff-only keyframe selection
  - A2 = DOM-mutation-only selection *(not implemented yet)*
  - A3 = pixel + DOM **fused** selection (DOM influences frame choice)
- **Factor B = prompt.** B0 = naive, B1 = dual-path decision (DOM evidence text
  in the prompt is strictly under B; it does not define A).

Consequence recorded above: the pilot's "ours" arm selected frames by pixels
only, so it was **A1B1, not A3B1**; the pilot table is relabelled A0B0 vs A1B1.

EXP002 conditions (**4 cells** — A1 = the existing selector, so no new
implementation for it): **A0B0, A1B1, A3B0, A3B1**. (A2 dropped from the
headline set; it would need a DOM-only selector not yet built.) Still to build
before EXP002 can be frozen: the A3 fused selector, per-call token capture in
`vlm_client`, and config-driven `run_trial.py` / `score_trial.py`.

**Verified: EXP001 has been run** — `poc/results/raw/` holds 32 pilot response
files (16 A0B0 + 16 A1B1). `experiments/exp001/` stays immutable; EXP002 is a
new experiment id, not an edit.

**Pre-registered power analysis** (`poc/power_analysis.py`, seed 20260727,
100k Monte-Carlo sims/cell). The paired test is an exact McNemar on discordant
pairs, so power depends on `(n, p_disc, psi)` — the discordant-pair rate and its
split — **not** on marginal accuracy. The pilot's psi=1.00 (b=3, c=0 at n=16) is
winner's-curse inflated and is explicitly not used as a planning basis; the
headline plan is **p_disc=0.20, psi=0.85**. Framed as *minimum detectable effect
at fixed n* (dataset_v3 is fixed at 120), not required-n.

| planning cell (p_disc=0.20, psi=0.85) | power |
|---|---|
| n=40 (exploratory) | 0.38 — below 0.80 |
| **n=120 (operating, all dataset_v3)** | **0.94 — meets 0.80** |

Decision: the n=40 exploratory grid was underpowered at the planning cell, so
**EXP002 evaluates all 120 paired clips**; no additional videos are generated.
Full sensitivity grids for both n written to `poc/results/power_analysis.json`.

### EXP002 build step 1 — A3 selector + config-driven run_trial

- **A3 fused keyframe selector** (`keyframes.py`, `select_fused`). Refactored the
  shared candidate-scoring + selection assembly out of `select_transition` (now
  A1) into `_candidate_scores` / `_build_selection`. Fusion rule (simple, one
  paragraph): a CSS-only defect changes pixels but produces no DOM mutation
  (injected stylesheet lives in `<head>`, unobserved), while real user actions
  do mutate the DOM — so A3 scores each candidate as
  `pixel_score / (1 + λ·dom_mutations_near)` and takes the argmax. DOM therefore
  genuinely changes *which* frame is sent. Verified on 6 buggy clips: A3 diverged
  from A1 on 4/6, jumping to a `dom_near=0` transition (the pure-rendering change)
  instead of the DOM-heavy user edit. Degenerates to A1 when no trace exists.
- **Token capture** (`vlm_client.call_json`): now returns `_usage`
  (prompt/candidates/total tokens) and accepts a `generation_config` (EXP002 pins
  temperature 0.0 through it instead of the SDK's default 1.0).
- **Config-driven `run_trial.py`**: removed the hardcoded `("baseline","ours")`;
  conditions, repeats, and generation config are read from a config
  (`experiments/exp002/conditions.draft.json`, `--config` / `--conditions` /
  `--repeats` overrides). Condition codes `AxBy` are parsed into factor levels;
  factor A picks frames (A0 uniform / A1 pixel / A3 fused), factor B picks the
  prompt (B0 naive / B1 dual-path, DOM evidence text strictly under B). Decodes
  once per clip; interleaves conditions×repeats for quota-safe pairing; writes
  `{id}__{code}__r{rep}.json` to `poc/results/exp002/` (kept separate from the
  EXP001 pilot raw dir). Loud validation rejects any unimplemented code (and
  A0+B1, which has no transition to describe).
- **B0 prompt fix**: dropped "sampled uniformly" from the naive prompt. Under
  A3B0 the frames are fused-selected, so that wording was a false claim and an
  ablation confound; B0 is now selection-agnostic. (Means EXP002's B0 differs by
  a few words from EXP001's frozen baseline — acceptable, new experiment.)
- **EXP002 conditions draft** (`experiments/exp002/conditions.draft.json`,
  clearly marked NOT frozen): the 4 cells A0B0/A1B1/A3B0/A3B1, factor
  definitions, repeats=3, generation_config (temp 0.0).

Smoke test (structural, no API key): `run_trial.py --dry-run --limit 2
--repeats 1` builds all 4 conditions on 2 clips → 8 valid JSON payloads; A0B0
sends 6 uniform frames, the rest send 3; B0 vs B1 prompts correct; A3B1 embeds
the DOM-activity block. Live JSON-from-model check still needs the API key.

Live smoke test (2026-07-28, model gemini-3.1-flash-lite): `run_trial.py
--limit 2 --repeats 1` made 8 real calls (2 control clips x 4 conditions). All 8
returned valid JSON verdicts (`normal`, correct — zero false positives), 0
unparseable, token usage + latency captured on every call. Input-token counts
already show the cost story: A0B0 (6 images) ~6662 vs the 3-image conditions
~3362-4000 tokens. Scratch results were deleted so the frozen run starts clean.

### EXP002 build step 2 — config-driven score_trial + statistics stack

Rewrote `score_trial.py` from the hardcoded 2-condition scorer into a
config-driven, multi-condition analyser reading the same conditions config as
`run_trial.py`. Repeats are collapsed by majority vote for the paired binary
tests; temperature 0.0 makes repeats mainly a probe of residual API
non-determinism.

Statistics implemented (each test records name, n, exact/asymptotic p, and an
effect size; everything written to `summary.json`, not just stdout):
- **Per condition**: accuracy mean +/- SD across repeats; bootstrap 95% CI
  (10k resamples, seed 20260728) over per-clip correctness *fractions* so the CI
  is coherent with the mean (verified it brackets the point estimate); FPR,
  recall, recall-by-bug-type; invalid-response count; defect-type match for B1
  conditions; mean tokens / latency / cost per clip.
- **Omnibus**: Cochran's Q over the per-clip correctness matrix (hand-rolled;
  chi-square p via scipy) — one test for "do the conditions differ at all".
- **Post-hoc accuracy**: pairwise exact McNemar + Holm-Bonferroni, effect size
  Cohen's g.
- **Post-hoc efficiency**: pairwise Wilcoxon signed-rank on tokens / latency /
  cost (per-clip means), effect size matched-pairs rank-biserial, Holm-corrected.
- **Cost**: derived from captured token usage x the pinned model's price
  (gemini-3.1-flash-lite $0.25 in / $1.50 out per 1M, from price_token.md);
  flags `price_assumed` if the model is unknown.

Verified end-to-end against a synthetic 40-clip x 4-condition x 3-repeat fixture
(scratchpad-only, never committed): Cochran's Q separated the conditions
(p=7e-05), McNemar+Holm and Wilcoxon+Holm produced sane pairwise output, and
every bootstrap CI bracketed its accuracy_mean. No real EXP002 data scored yet.

### EXP002 frozen (2026-07-30)

Promoted the draft to `experiments/exp002/config.frozen.json` (status: frozen,
frozen_from_commit ef3344c), mirroring EXP001's structure. Pins: the 4-cell
factorial (A0B0/A1B1/A3B0/A3B1) with factor + fusion definitions; model
gemini-3.1-flash-lite; generation_config (temp 0.0, top_p 1.0, top_k 1,
max_output_tokens 512) — now actually wired, unlike EXP001 which froze it as a
TODO; repeats 3; prompt SHA-256s; A3 fusion params (fuse_lambda 1.0, window
1200ms); dataset_v3 (120 clips, full manifest); the statistics stack (Cochran's Q
-> McNemar+Holm -> Wilcoxon+Holm, bootstrap seed 20260728); the cost model
(price_token.md); and the power-analysis reference (planning power 0.94 at n=120).

- **B0 prompt SHA differs from EXP001** by design (baseline/v2, dropped "sampled
  uniformly"); B1 and the expected-appearance block are unchanged (byte-identical
  SHAs to EXP001). Verified all three SHAs match the live code.
- **Runners repointed**: `run_trial.py` / `score_trial.py` DEFAULT_CONFIG now
  reads `config.frozen.json`; the superseded `conditions.draft.json` was removed.
- **freeze_policy.unfrozen_by_design**: `score_trial.py` only (pure function of
  cached raw responses).
- Verified: dry-run reads the frozen config and builds all 4 conditions; both
  runners load it; prompt SHA integrity check passes.

EXP002 is a NEW experiment id (not an edit of EXP001, which stays immutable).

### Pre-run refactor of `poc/` (branch: refactor/poc-cleanup, 2026-07-30)

Structural cleanup taken deliberately *before* the 1440-call trial, so the code
that produces the reported numbers is the tidy version and no refactor lands
mid-collection (which the freeze policy forbids for everything except
`score_trial.py`). **Explicit constraint: zero behavioural change** — no method,
threshold, statistic or prompt was touched.

**Prompt used** (verbatim, translated): "before running the formal experiment,
open a branch and refactor the code for me — tidy-up only, do not change the
method logic; then can you delete the datasets on the remote without touching
history?" Scope was then narrowed by an explicit choice of "structural tidy-up +
extract shared modules" over "conservative cleanup only".

Three shared modules extracted from duplication that had accumulated across the
six entry-point scripts:
- `poc/paths.py` — repo-relative paths and JSON read/write, resolved from
  `__file__` rather than the CWD. `POC_DIR`/`REPO_ROOT` had been re-derived in
  five files, and the `json.dumps(..., indent=2) + "\n"` write idiom in four.
- `poc/conditions.py` — the `AxBy` grammar, the "is this level implemented"
  validator, and the frozen-config loader. `run_trial.py` and `score_trial.py`
  each had their own regex and their own loader with divergent error messages;
  `score_trial.py` previously did no validation at all.
- `poc/stats_tests.py` — Cochran's Q, exact McNemar, Cohen's g, Wilcoxon
  signed-rank, Holm-Bonferroni, bootstrap CI. The exact-McNemar formula existed
  **twice**, in `score_trial.py` and `power_analysis.py`, with only a comment
  asserting they matched. They now share one function, so the pre-registered
  power model is provably a model of the test that will actually be applied.

Per-file work:
- `build_manifest.py` — parsed `sys.argv` **at import time** and derived
  `MODE` / `DATASET_DIR` / `DATASET_NAME` as module globals, so importing it
  from a test or another script would consume the caller's argv. Replaced with a
  frozen `BuildSpec` dataclass threaded through as an argument. Removed the dead
  `by_id` dict and its `assert by_id  # silence linters`.
- `score_trial.py` — a 165-line `main()` split into `score_condition()` /
  `omnibus()` / `posthoc_accuracy()` / `posthoc_efficiency()`, with the
  per-condition state carried by a `ConditionScore` dataclass instead of four
  parallel dicts. Deleted the unused `wilson()` helper (dead since the switch to
  the bootstrap CI — a live Wilson function in a stats module invites someone to
  report a CI the paper does not use) and an unused `CONDITION_RE`.
- `run_trial.py` — extracted `Session`, `process_clip()`, `save_dry_record()`,
  `save_call_record()`, `resolve_config()`. The prompt templates deliberately
  stay in this file: the frozen config pins them by SHA-256 *and* by source path
  (`poc/run_trial.py::OURS_PROMPT`), so moving them would break the integrity
  reference.
- `smoke_test.py` — was re-implementing `get_api_key`, `is_quota_error` and
  `_strip_code_fence` verbatim from `vlm_client.py`; now imports them, so a green
  smoke test is evidence about the real pipeline rather than about a copy.
- `vlm_client.py` — `numpy`/`Pillow` imports deferred into `frame_to_part()`
  (the only consumer), matching `keyframes.decode()`'s existing lazy-import
  style. This is what makes the `smoke_test` import safe: smoke_test's job is to
  *diagnose* a broken environment, so it must not hard-fail at import time on the
  very packages it was written to report on.
- `gate_videos.py` — `diff_ratio` renamed `yiq_diff_ratio`. It shares a name with
  `keyframes.diff_ratio` but computes a different metric (pixelmatch YIQ colour
  delta vs a plain RGB threshold); the two are not interchangeable and the
  collision was a live trap.
- `poc/.gitignore` — `results/raw/DRY__*` only covered EXP001's layout, so
  EXP002 dry-run payloads under `results/exp002/raw/` were not ignored. Added
  `results/*/raw/DRY__*`.

**Verification — golden-output diffing.** Every artefact was captured before the
refactor and byte-compared after:

| check | scale | result |
|---|---|---|
| frozen prompt SHA-256 vs `config.frozen.json` | 3 templates | identical |
| `manifest.json` / `manifest.csv` | 120 clips | identical |
| `power_analysis.json` | 2 grids x 20 cells | identical |
| `summary.json` + stdout | 1439 synthetic responses | identical |
| `run_trial --dry-run` payload files + stdout | 3 clips x 4 conds x 3 reps | identical |
| payload prompt **and decoded-frame pixel** hashes | 14 clips x 4 conds | identical |
| `video-gate-report.json` | 120 clips | identical |

The frame-pixel hashes are the load-bearing check: they would catch any drift in
decoding, A1/A3 selection, or downscaling that a prompt-text diff would miss.
Negative controls also re-run: `A0B1`, `A9B0`, `A1B9` and `notacode` are still
rejected with their specific reasons, and `--repeats` / `--conditions` overrides
still interleave correctly.

**Not done, and why**: the ~34MB of `dataset_v1`/`v2` blobs in git history stay.
`origin/main` already tracks zero dataset files (untracked in `ace7c63`, all
three versions gitignored), so nothing is deletable at HEAD; the historical blobs
are only removable by a history rewrite, which was explicitly ruled out.

### Next Steps
- [x] Freeze `experiments/exp002/config.frozen.json` — done 2026-07-30.
- [x] Refactor `poc/` before collection starts — done 2026-07-30.
- [ ] Run the full trial: `python3 poc/run_trial.py` (120 x 4 x 3 = 1440 calls,
      resumable) with `GEMINI_MODEL` + `GOOGLE_API_KEY` set, then
      `python3 poc/score_trial.py`.
- [ ] Refresh `poc/README.md` end to end — its Stage 1/3 sections still describe
      the EXP001 16-clip, 2-condition pre-trial.

---

## Appendix — MR description convention (reusable template)

How to write a merge-request description in this project. Reuse this skeleton
for every MR; fill each section and keep the *why*, not only the *what*.

### Section structure

1. **Title** — one line: what it does, plus a scope flag when relevant
   (e.g. "no results — pre-registration").
2. **## Summary** — 2-4 sentences framed as *problem -> change*: the gap or
   defect, then what this MR ships to close it.
3. **## Changes** — one `###` subsection per area. Each bullet states the
   rationale / root cause, not just the edit, and cites exact file paths and
   constants / thresholds.
4. **## Results** — a table when the MR produces numbers; always give `n` and
   say whether it is a *sanity check* or a *claim*. If there are none, write
   "no results (pre-registration)".
5. **## Known limitations** — what is deferred and why; mark
   negative-by-design vs follow-up.
6. **## GenAI Usage** — four bullets: **Tools** / **AI contributions** /
   **Human ownership** (name the concrete decisions that were yours, not the
   AI's) / **Prompt records** (point at this worklog).
7. **## Test Plan** — a checkbox list of verifications, including at least one
   *negative control* (a check that a guard rejects what it was built to catch).

### Conventions

- Every Changes bullet answers "why", not only "what".
- Cite exact constants, thresholds, and file paths.
- Separate sanity checks from publishable claims — always state `n`.
- Prefer past evidence: link the commits/tests that back each claim.
