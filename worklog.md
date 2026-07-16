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
- [ ] Build the PoC evaluation runner: feed each manifest clip/keyframes to
      `gemini-3.5-flash`, collect verdicts, and score against `ground_truth`
      (accuracy, false-positive rate on the 8 normal clips, per-bug-type recall).
