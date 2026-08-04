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

- **Stage 1 — ground-truth manifest** (`pipeline/build_manifest.py`):
  - Generates `data/manifest.json` + `data/manifest.csv` from `dataset_v1/metadata.json`.
  - Balanced pre-trial subset: **16 videos, 8 normal / 8 anomalous**, all six bug
    types covered (blur & skew sampled x2 as the hardest, lowest-diff cases).
  - Labels/expectations are auto-derived from the injected bug key (not hand-written).
  - Guards enforce the requirements (10-20 videos, ~50/50 split, asset existence).
- **Stage 2 — four-stage smoke test** (`pipeline/smoke_test.py`): NETWORK -> AUTH ->
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
- New **video-level quality gate** (`pipeline/gate_videos.py`): last decoded frame
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

- `pipeline/keyframes.py` — decode + uniform sampling (baseline) and sustained-
  transition change-point selection + final settled frame (ours). Pixel-only
  probe confirmed the classes are NOT separable by change magnitude (normal
  typing produces bigger diffs than several bugs), so the structural signal
  does not leak the answer.
- `pipeline/run_trial.py` — both conditions per clip, interleaved, cached in
  results/exp001/raw/ (quota-crash resumable); cheapest model frozen for the whole
  trial; per-clip ground truth never enters any prompt. `--dry-run` builds all
  payloads without API calls.
- `pipeline/score_trial.py` — accuracy + Wilson CI, FPR on normals, per-bug recall,
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
(`generator/tests/bug-injection.spec.ts`, CONTROL_CASES 60, CASES_PER_BUG 10)
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
(`dataset_v3/video-gate-report.json`, produced by `pipeline/gate_videos.py`).
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
clips, subsetting bypassed). `data/manifest.json` still pointed at dataset_v2.

Changes to `pipeline/build_manifest.py`:
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

Regenerated: `data/manifest.json` + `data/manifest.csv` now describe the full
dataset_v3 (120 clips, 60/60, 10 per type). Verified: every bug type maps to
exactly one distinct expectation string (label auto-derivation intact), all
downstream-required fields present on all 120 cases, zero
`ground_truth`/`is_anomaly` mismatches, and pretrial mode still produces the
original 16-clip subset for v2.

Command:

    python3 pipeline/build_manifest.py dataset_v3

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

**Verified: EXP001 has been run** — `results/exp001/raw/` holds 32 pilot response
files (16 A0B0 + 16 A1B1). `experiments/exp001/` stays immutable; EXP002 is a
new experiment id, not an edit.

**Pre-registered power analysis** (`pipeline/power_analysis.py`, seed 20260727,
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
Full sensitivity grids for both n written to `results/power_analysis.json`.

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
  `{id}__{code}__r{rep}.json` to `results/exp002/` (kept separate from the
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

### Pre-run refactor of `pipeline/` (branch: refactor/poc-cleanup, 2026-07-30)

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
- `pipeline/paths.py` — repo-relative paths and JSON read/write, resolved from
  `__file__` rather than the CWD. The pipeline-dir and repo-root constants had
  been re-derived in five files, and the `json.dumps(..., indent=2) + "\n"`
  write idiom in four.
- `pipeline/conditions.py` — the `AxBy` grammar, the "is this level implemented"
  validator, and the frozen-config loader. `run_trial.py` and `score_trial.py`
  each had their own regex and their own loader with divergent error messages;
  `score_trial.py` previously did no validation at all.
- `pipeline/stats_tests.py` — Cochran's Q, exact McNemar, Cohen's g, Wilcoxon
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
  (`pipeline/run_trial.py::OURS_PROMPT`), so moving them would break the integrity
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
- `pipeline/.gitignore` — `results/raw/DRY__*` only covered EXP001's layout, so
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

### Repository restructure — directory naming and lifecycle separation (2026-07-30)

**Prompt used** (verbatim, translated): "but I think the current file and folder
structure is ugly, and it's the formal experiment now but the folders and
filenames still say poc — can you deal with that?" Two follow-up choices were
made explicitly: `pipeline/` + `generator/` as the new names, and "rename **and**
hoist data/results to the top level" over "rename only".

The names had stopped describing their contents. `poc/` held the apparatus for
the formal factorial ablation, not a proof of concept, and `latest/` never
described anything at all — it is the Playwright fault-injection suite. Worse,
`poc/` mixed three things with different lifecycles: code that should be freely
refactorable, dataset-derived ground truth that is regenerable, and collected
model responses that are evidence and effectively append-only.

| before | after | why |
|---|---|---|
| `poc/*.py` | `pipeline/` | it is the evaluation pipeline, not a PoC |
| `latest/` | `generator/` | it generates the dataset |
| `latest/tests/poc-bug-injection.spec.ts` | `generator/tests/bug-injection.spec.ts` | same |
| `poc/manifest.{json,csv}` | `data/` | ground truth is data, not code |
| `poc/results/raw/` (EXP001) | `results/exp001/raw/` | see below |
| `poc/results/exp002/raw/` | `results/exp002/raw/` | |
| `poc/results/power_analysis.json` | `results/power_analysis.json` | |

EXP001's results sat *flat* in `results/raw/` while EXP002's would nest under
`results/exp002/raw/` — a layout inconsistency that had already leaked into
`.gitignore` as two parallel `DRY__*` patterns. Normalising EXP001 to
`results/exp001/` collapses those back to one rule. All moves were made with
`git mv`, so every file is recorded as a rename and its history follows; the 32
EXP001 raw responses show as pure renames with no content change.

**Timing was the deciding constraint.** `results/exp002/` did not yet exist —
EXP002 had issued zero API calls. The freeze policy forbids changing a frozen
config *while results are being collected*, so doing this now cost nothing;
doing it after even one call would have required a new `experiment_id`.

**Frozen configs were amended, not silently edited.** Both
`experiments/exp00{1,2}/config.frozen.json` pin paths — including the prompt
integrity fields, which pin each template by SHA-256 **and** by source path
(`poc/run_trial.py::OURS_PROMPT`). Leaving them would have left the specs
pointing at directories that no longer exist. Each file now carries a
`path_layout_migration` block recording the date, the reason, the full old→new
mapping, and — for EXP002 — that zero results existed at the time. Nothing else
in either file changed.

**Verification.** The same golden-output diffing used for the refactor was
re-run in the new layout, against the identical pre-refactor baselines:

| check | scale | result |
|---|---|---|
| `manifest.json` / `manifest.csv` | 120 clips | identical |
| `power_analysis.json` | 2 grids x 20 cells | identical |
| `summary.json` + stdout | 1439 synthetic responses | identical |
| `run_trial --dry-run` payload files | 3 clips x 4 conds x 3 reps | identical |
| payload prompt **and decoded-frame pixel** hashes | 14 clips x 4 conds | identical |
| `video-gate-report.json` + stdout | 120 clips | identical |
| prompt SHA-256 vs `exp002/config.frozen.json` | 3 templates | all verify |
| negative controls (`A0B1`/`A9B0`/`A1B9`/`notacode`) | 4 | still rejected correctly |

The only observable difference anywhere was one line of `run_trial` stdout
printing the new absolute results path.

**One pre-existing mismatch found, not caused by this change.** EXP001's config
pins `baseline` at `4ede1440…` while the current `BASELINE_PROMPT` hashes to
`f6db8781…`. Extracting the literal from `main` by AST confirmed the template is
byte-identical before and after the restructure: the naive prompt was
deliberately revised at the EXP001→EXP002 boundary, which is precisely why
EXP002 is a new `experiment_id`. EXP001's config correctly records the prompt as
it was when EXP001 ran, so the mismatch is the freeze mechanism working.

**Also noted, not acted on**: `CLAUDE.md` is listed in `.gitignore` but was
tracked before that rule was added, so it is still in the repo and on the remote
— `.gitignore` does not untrack an existing file. A comment now says so at the
point of confusion rather than leaving the file looking excluded.

### Next Steps
- [x] Freeze `experiments/exp002/config.frozen.json` — done 2026-07-30.
- [x] Refactor `pipeline/` before collection starts — done 2026-07-30.
- [x] Rename `poc/`→`pipeline/`, `latest/`→`generator/`; hoist `data/` and
      `results/` to the top level — done 2026-07-30, before any EXP002 call.
- [ ] Run the full trial: `python3 pipeline/run_trial.py` (120 x 4 x 3 = 1440 calls,
      resumable) with `GEMINI_MODEL` + `GOOGLE_API_KEY` set, then
      `python3 pipeline/score_trial.py`.
- [ ] Refresh `pipeline/README.md` end to end — its Stage 1/3 sections still describe
      the EXP001 16-clip, 2-condition pre-trial.
- [ ] Decide whether `CLAUDE.md` should be untracked (`git rm --cached CLAUDE.md`).

## 2026-07-31

### EXP002 formal trial — pre-collection checks and payment setup

**Branch**: `exp/formal-trial-v1` (created 2026-07-31, matches frozen config's
`branch` field).

#### Precondition verification

All ground-truth and code elements passed verification before API access:

- **Dataset integrity**: dataset_v3 = **120 clips** (60 normal, 60 anomaly),
  10 per bug type; all 120 `.webm` + all 120 `domtrace.json` present and readable.
- **Manifest**: `data/manifest.json` schema v2, `selection_mode: full`, 120 records
  with derived labels from injected bug keys.
- **Prompt SHA-256 verification** (vs `experiments/exp002/config.frozen.json`):
  - `B0_naive`: got `f6db8781…` = want `f6db8781…` ✓
  - `B1_dual_path`: got `9f42bb8a…` = want `9f42bb8a…` ✓
  - `expected_appearance_block`: got `0a8ffb24…` = want `0a8ffb24…` ✓
- **Dry-run payload build** (`--dry-run --limit 2`): 24 payloads generated
  correctly — A0=6 frames, A1/A3=3 frames, 4 conditions × 3 repeats interleaved.

#### Stage 2 smoke test (4 gates: NETWORK / AUTH / IMAGE / JSON)

```
[PASS] Stage 1 NETWORK — reaching generativelanguage.googleapis.com (HTTP 404 OK)
[PASS] Stage 2 AUTH — API key accepted, 42 models in account
[PASS] Stage 3 IMAGE — model saw buggy PNG frame, generated description
[PASS] Stage 4 JSON — model returned parseable JSON verdict
```

Working judge: **`gemini-3.1-flash-lite`** (pinned by frozen config).

#### Stage 3 trial probe — real API calls on 2 clips (24 calls)

Ran `python3 pipeline/run_trial.py --limit 2` **on the free tier** to measure
actual token usage and confirm the response format before committing to 1440
calls. Token counts are read from each response's `usage_metadata`, not
estimated:

| condition | n_calls | mean_input_tokens | mean_output_tokens |
|---|---|---|---|
| A0B0 (uniform, 6 frames) | 6 | 6,662 | 36 |
| A1B1 (pixel keyframes, 3) | 6 | 3,998 | 47 |
| A3B0 (fused keyframes, 3) | 6 | 3,362 | 36 |
| A3B1 (fused keyframes, 3) | 6 | 3,992 | 41 |

Input tokens scale with frame count as expected: A0B0's 6 frames cost ~1.7x the
3-frame cells. The B0/B1 gap within fused selection (3,362 -> 3,992) is the
dual-path prompt's own text.

**Latency from this probe is NOT reported** — it is uninterpretable. A free-tier
429 fired mid-probe and `vlm_client` slept 20s + 40s inside the call being timed,
inflating that cell's mean by ~10s/call. Real latency comes from the full paid
run, where it is a genuine efficiency metric.

**Projected full run** (120 clips x 4 conditions x 3 repeats = 1440 calls),
extrapolating the measured per-condition means:
- input **6.485M** tokens, output **58.2k** tokens
- modelled cost at the frozen config's rate card ($0.25 / $1.50 per 1M): **$1.71**

Note this projection corrected an earlier back-of-envelope guess of ~$0.5, which
had underestimated image tokens by more than half. The measured figure supersedes
it.

**Tier-consistency note**: those 24 probe responses were produced on the free
tier. They were **deleted** before the formal run rather than reused. The freeze
policy requires every cell to see the same judge, and while the model id is
pinned, free vs paid serving paths are not something this project can verify are
identical — cheaper to re-run 2 clips (~$0.03) than to leave an unanswerable
question in the results. `results/exp002/raw/` was empty at launch (verified).

#### Billing

The key's project was on the free tier at probe time. Attempting the full run
after enabling billing aborted immediately at the model-probe step — zero calls
issued, empty cache, no partial state — with a **different** 429 than before:

```
"Your prepayment credits are depleted. Please go to AI Studio at
 https://ai.studio/projects to manage your project and billing."
```

i.e. billing was linked as **prepay** with a zero balance, so every request was
rejected outright. Diagnosed by calling `generateContent` directly with `curl`;
the runner's own message ("Every candidate model is quota-exhausted") would have
been read as a free-tier rate limit and sent the investigation the wrong way.

After top-up (minimum £10.00 on this account), the same probe returned
`"serviceTier": "standard"` and `"modelVersion": "gemini-3.1-flash-lite"` —
confirming both that billing was live and that the pinned judge was being served.

#### Formal run launched

`python3 pipeline/run_trial.py` started 2026-07-31 14:25 on branch
`exp/formal-trial-v1`, full 1440 calls, single paid session, responses cached
per call under `results/exp002/raw/`.

#### Run completed

All 1440 calls landed: 360 per condition, 120 per (condition, repeat) cell, no
gaps and no re-runs. Every response reports `model: gemini-3.1-flash-lite` — the
runner's abort-on-unavailable rule was never triggered, so no cell saw a
different judge.

| | projected from the 24-call probe | actual |
|---|---|---|
| input tokens | 6.485M | 6.492M |
| output tokens | 58.2k | 58.4k |
| cost | $1.71 | **$1.71** |

The probe's measured per-condition token means extrapolated to within 0.1%. This
is the payoff from reading `usage_metadata` off real responses instead of
estimating image tokens — the earlier back-of-envelope guess of ~$0.5 was out by
more than 3x.

## 2026-08-02

### EXP002 scoring and analysis

`python3 pipeline/score_trial.py` over the cached responses, writing
`results/exp002/summary.json` (run 2026-07-31 14:59, immediately after
collection; reviewed and written up here). Scoring is a pure function of the
cache, so this step is re-runnable without spending anything.

#### Headline numbers

| Condition | Accuracy ± SD [95% CI] (n=120) | FPR (n=60) | Recall (n=60) | Mean tokens | Median latency |
|---|---|---|---|---|---|
| A0B0 (uniform + naive) | 0.753 ± 0.005 [0.675, 0.828] | 0.167 | 0.667 | 6697 | 1.097s |
| A1B1 (pixel keyframes + dual-path) | **0.908 ± 0.000** [0.850, 0.958] | **0.000** | **0.817** | 4031 | 0.983s |
| A3B0 (fused + naive) | 0.756 ± 0.005 [0.678, 0.831] | 0.117 | 0.633 | **3396** | 0.952s |
| A3B1 (fused + dual-path) | 0.842 ± 0.008 [0.775, 0.903] | 0.000 | 0.683 | 4072 | 0.903s |

SD is across the 3 repeats; CIs are 10,000-resample bootstraps over clips
(seed 20260728). Omnibus Cochran's Q = 23.30, df = 3, n = 120, p = 3.5e-05.

Pairwise exact McNemar, Holm-corrected, on the per-clip majority vote:

| contrast | n_disc | split (b/c) | p_holm | Cohen's g |
|---|---|---|---|---|
| A0B0 → A1B1 | 21 | 1 / 20 | **0.000126** | 0.45 |
| A0B0 → A3B0 | 23 | 11 / 12 | 1.0 | 0.02 |
| A0B0 → A3B1 | 25 | 7 / 18 | 0.130 | 0.22 |
| A1B1 → A3B0 | 24 | 21 / 3 | **0.001385** | 0.38 |
| A1B1 → A3B1 | 8 | 8 / 0 | **0.031** | 0.50 |
| A3B0 → A3B1 | 24 | 7 / 17 | 0.130 | 0.21 |

Efficiency (Wilcoxon signed-rank, Holm-corrected, 3 metrics x 6 pairs): every
selector cell beats A0B0 on tokens and cost at p_holm < 1e-6 with rank-biserial
1.00. A1B1 reaches its accuracy on 40% fewer tokens than the baseline, so the
accuracy gain is not bought with extra compute.

#### Three findings, two of them negative

1. **The A3 fused selector buys no accuracy.** A0B0 → A3B0 is 0.753 → 0.756,
   p_holm = 1.0, g = 0.02. It halves token usage (6697 → 3396, −49%), but that
   saving comes from sending 3 frames instead of 6, which pixel-only selection
   also does — it cannot be attributed to the DOM fusion.
2. **Fusion actively harms the method under the dual-path prompt.** A1B1 (0.908)
   vs A3B1 (0.842), p_holm = 0.031, g = 0.50, and **all 8 discordant clips
   favour A1B1 with none favouring A3B1**. Per-type at fixed B1: flicker_seizure
   8/10 → 5/10, flyaway_offset 10/10 → 8/10, and blur_unreadable /
   skew_distortion / displaynone_collapse each lose one clip. Three of the eight
   discordant clips are flicker_seizure.
3. **The headline gain is real but aliased.** A0B0 → A1B1 is 0.753 → 0.908
   (p_holm = 0.000126) with false positives 10/60 → 0/60, but that contrast moves
   both factors at once. The clean prompt-only contrast A3B0 → A3B1 does not
   survive correction (p_holm = 0.130), and the frozen design contains no A1B0
   cell, so the gain cannot be decomposed.

`displaynone_collapse` stays a blind spot: correct in only **2 of 40**
clip-conditions. The pilot contained a single clip of this type and it also
failed, so the n=16 pre-trial could not have detected this — it took 10 clips per
type to see that it is systematic rather than incidental.

#### Analysis-time discoveries

- **The one "invalid" response is not a parse failure.** `bug_flicker_seizure_5`
  under A3B1 repeat 2 returned well-formed JSON whose `reason` correctly
  described the defect, but with the out-of-schema verdict string `"defect"`
  instead of `"anomaly"`. `score_trial.py` counts anything outside
  {normal, anomaly} as wrong. It changed no result — the other two repeats of
  that clip both said `"normal"`, so the majority vote was already wrong — but it
  means detection rate and schema-compliance rate are different quantities and
  must not be conflated in the write-up.
- **`summary.json` mixes two aggregation units.** `accuracy_mean` is the mean of
  the three per-repeat accuracies; FPR, recall, Cochran's Q and every McNemar
  test run on the per-clip **majority vote**. So A0B0 → A3B0 reads +0.003 on the
  accuracy scale but +1/120 = +0.008 on the majority-vote scale, and A0B0's FPR
  is 10/60 = 0.167 clip-level against 29/180 = 0.161 response-level. Any table
  quoting these must say which unit it uses.
- **Mean latency is unsafe for A3B1.** One clip took 39.97s, lifting the mean to
  1.261s — above A0B0's 1.140s — while the median is 0.903s and the rank-based
  Wilcoxon puts A3B1 *faster* than baseline at rank-biserial 0.86. Reporting the
  mean would state the opposite of the test. Medians are used throughout.
- **The A3 null is a net null, not an absence of effect.** A0B0 → A3B0 is
  accuracy-neutral yet flips 23 of 120 clips, with large offsetting per-type
  swings (blur_unreadable 10/10 → 6/10, skew_distortion 6/10 → 3/10,
  flyaway_offset 6/10 → 8/10, vanish_invisible 9/10 → 10/10). "Contributes
  nothing" would be the wrong summary; "no net accuracy effect at half the token
  cost" is the accurate one.
- **The observed A3 failure direction contradicts its design rationale.** The
  fusion rule down-weights pixel transitions that coincide with DOM activity
  specifically to *favour* CSS-only defects, which is where it now loses. No
  verified mechanism yet — a frame-level comparison of what A1 and A3 select on
  the 8 discordant clips is queued before the Results chapter is written.
- **`statsmodels` is absent from the locked environment**, so Cochran's Q is
  implemented directly in `stats_tests.py` while scipy 1.17.1 supplies Wilcoxon
  and the chi-square tail. This deviates from `Satistic_Display_Rule.md` §2 and
  is disclosed wherever Q is reported.
- **Dispersion gap against the supervisor rule.** §1 requires mean ± SD [95% CI]
  (n) for *every* metric, but `score_trial.py` currently emits SD and CI for
  accuracy only — FPR, recall and defect_type_match are bare point estimates.
  The missing figures were computed off the cache for the supervisor report;
  folding them into the scorer is a follow-up (the frozen config lists
  `score_trial.py` under `unfrozen_by_design`, so this needs no re-run).

#### Decisions taken

- **Thesis contribution reframed.** `config.frozen.json` designates A3B1 as "the
  full proposed method" and A1B1 as "the pilot's 'ours'". Given findings 1 and 2,
  the write-up will report **A1B1 as the proposed method and A3 as a falsified
  design**. This is a post-hoc change of the designated contribution, recorded
  here with a date rather than applied silently to the frozen file — anyone
  comparing the frozen config against the thesis will see the switch and its
  reason.
- **A1B0 will not be run.** It would de-alias contrast 3 and cost roughly $0.32,
  but it would be a post-hoc addition driven by the results, and the freeze
  policy requires a new experiment id rather than an amendment. The
  pre-registered design stays intact; the aliasing goes in Limitations, together
  with its real consequence: **the evidence that fusion harms the method rests
  solely on the B1 layer and is uncorroborated at B0.**
- **Cross-model comparison with Qwen2.5-VL dropped.** The VLM is fixed as a
  controlled variable across all conditions; the contribution is the keyframe
  selection and decision layers, and holding the judge constant strengthens that
  claim. Recorded as future work. Note this removes a comparison that
  `Satistic_Display_Rule.md` §2 explicitly anticipates, and `README.md` still
  describes Path A as using Qwen — that needs updating.

#### Supervisor report

Progress email sent to Dr Chen with the four tables (per-condition metrics,
resource utilisation, Wilcoxon, McNemar), the three findings and the two open
questions. Three reporting errors were caught during a line-by-line
reconciliation against `summary.json` and the raw cache **before** it was sent:

1. A per-type recall figure attributed to the wrong condition — "10/10 → 3/10 on
   `skew_distortion`" is A1B1 vs **A3B0**, a cross-factor comparison; at fixed B1
   the same type only goes 10/10 → 9/10. The real loss is `flicker_seizure`.
2. The false-positive rate quoted against the wrong denominator ("0.167 across
   180 normal responses"); 0.167 is 10/60 clip-level, and the response-level
   figure is 0.161.
3. The invalid response described as "unparseable" when it was a schema-
   compliance failure (see above).

#### GenAI record (CLAUDE.md documentation requirement)

- **Tool**: Claude Code (Opus 5), 2026-08-02 to 2026-08-04.
- **Prompts**: "check my email against what the experiment actually produced";
  then requests to rewrite the affected passages, to build tables meeting
  `Satistic_Display_Rule.md` §1/§3, and to judge what belonged in this commit.
- **AI contribution**: the reconciliation that surfaced the three errors above
  and the analysis-time discoveries in this section; drafting of the corrected
  email passages and of the dispersion figures reported to the supervisor.
- **Human ownership**: the contribution reframe, the decision not to run A1B0,
  the decision to drop the cross-model comparison, and what was sent to the
  supervisor were all decided here, not by the model. Several AI suggestions were
  rejected — notably the proposal to run A1B0 as a declared supplementary
  experiment.
- **Verification**: every figure in this section was recomputed from
  `results/exp002/raw/` and checked against `summary.json`; no statistic in the
  repository was produced or edited by the model.

### Next Steps

- [ ] Frame-level comparison of A1 vs A3 selections on the 8 discordant clips —
      needed before the mechanism for finding 2 can be asserted.
- [ ] Extend `score_trial.py` to emit SD and bootstrap CI for FPR, recall and
      defect_type_match, so §1 compliance is traceable in `summary.json` rather
      than recomputed ad hoc. No re-run required.
- [ ] Update `README.md`: Path A still lists Qwen2.5-VL as a judge.
- [ ] Write Limitations around the contrast-3 aliasing, the B0-uncorroborated
      fusion result, and the `displaynone_collapse` blind spot.
- [ ] Decide whether `CLAUDE.md` should be untracked (`git rm --cached CLAUDE.md`).

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
