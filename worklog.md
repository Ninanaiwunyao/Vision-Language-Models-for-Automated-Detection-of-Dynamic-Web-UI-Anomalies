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

| metric | baseline (uniform x6 + naive) | ours (smart keyframes x3 + dual-path) |
|---|---|---|
| accuracy | 0.75 [CI 0.51-0.90] | **0.94** [CI 0.72-0.99] |
| false-positive rate (8 normal) | 0.125 | **0.0** |
| anomaly recall (8 buggy) | 0.625 | **0.875** |
| defect-type match | — | 5/7 |

Paired: ours-only-correct **3**, baseline-only-correct **0** (strict dominance;
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
- [ ] Rebuild the manifest against dataset_v3 — this is **not** just a
      re-point. `poc/manifest.json` still resolves to dataset_v2 throughout,
      and `poc/build_manifest.py` blocks a full-scale manifest three ways:
      its dataset auto-discovery list is `("dataset_v2", "dataset_v1")` and
      never sees v3; it hard-fails any subset outside 10-20 clips (line 164);
      and its selection plan is hardcoded to the pre-trial's 8 anomalous + 8
      normal. The metadata schema itself is fine — v3's case keys are a strict
      superset of v2's — so the work is in the selection/guard logic, not the
      reader.
