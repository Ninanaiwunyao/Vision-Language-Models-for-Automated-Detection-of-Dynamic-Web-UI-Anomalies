# PoC Pre-Trial (`poc/`)

Two gated stages that must both be green **before** running the real VLM
evaluation. If they are not, an accuracy number is unattributable — you can't
tell "the method doesn't work" from "the pipeline is broken".

## Stage 1 — Ground-truth manifest

```bash
python3 poc/build_manifest.py
```

Reads `dataset_v1/metadata.json` (produced by the Playwright fault-injection
matrix) and emits a **balanced subset** for the pre-trial:

- `poc/manifest.json` — rich, machine-readable ground truth (one object/video)
- `poc/manifest.csv` — flat view for quick inspection

Current subset: **16 videos, 8 normal / 8 anomalous**, all six bug types
covered (the two hardest, lowest-diff types — `blur` and `skew` — are sampled
twice). This satisfies the three requirements: 10–20 videos, ~50/50 split (so a
false-positive rate is meaningful), and a ground-truth label on every clip.

Every label is **derived, not hand-written**: because each bug is a known CSS
mutation injected by `latest/tests/poc-bug-injection.spec.ts`, the
`ground_truth` (`normal`/`anomaly`) and the `expectation` symptom text come
straight from the bug key. To change the subset, edit `N_CONTROLS` /
`BUG_SELECTION` in `build_manifest.py` and re-run.

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
| `keyframes` | `normal` / `buggy` / `diff` PNG paths |
| `diff_ratio` | measured pixel-diff ratio (severity evidence) |
| `min_diff_ratio` | the quality-gate threshold this clip cleared |

## Stage 2 — Four-stage smoke test

```bash
export GOOGLE_API_KEY=...            # or GEMINI_API_KEY
python3 poc/smoke_test.py            # exit 0 = gate GREEN
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

Unless you pin one with `GEMINI_MODEL=...`, stages 3-4 try a prioritised list of
`*-flash` models and **fall back to the next on a `429` quota error**
(`gemini-2.5-flash` first, `gemini-2.0-flash` last). This matters because the
free tier can be zeroed out (`limit: 0`) for a *specific* model while others on
the same key still work.

If **every** candidate returns 429, the key/project has no free-tier quota left:
enable billing on the Google Cloud project, wait for the daily reset, or set
`GEMINI_MODEL` to a model you have quota for.

## Stage 3 — Paired two-condition trial (the PoC core)

```bash
python3 poc/run_trial.py --dry-run   # build every payload, zero API calls
python3 poc/run_trial.py             # the real thing (needs GOOGLE_API_KEY)
python3 poc/score_trial.py           # metrics + McNemar paired test
```

Every manifest clip runs under BOTH conditions (a paired design):

| | sampling | images | prompt |
|---|---|---|---|
| **baseline** | uniform over the clip | 6 | naive "is there a visual bug?" |
| **ours** | strongest sustained pixel transition (before/after) + final settled state | 3 | dual-path: generic expected-appearance reference + structural pixel signals + DOM-mutation correlation |

Fairness rules (enforced in code):
- One model — the **cheapest** available (`*-flash-lite` tier first) — resolved
  once and frozen for all calls; 429s retry the same model, never switch.
- The manifest's per-clip `expectation` never enters any prompt; the expected-
  appearance text in `ours` is generic and identical for every clip.
- Keyframe selection reads only pixels/DOM, never the label.
- Conditions are interleaved per clip, and responses are cached in
  `poc/results/raw/` — a quota crash mid-run resumes without losing pairing.

`score_trial.py` reports accuracy (with Wilson 95% CI), false-positive rate on
the normal clips, per-bug-type recall, and an exact McNemar test on the
discordant pairs. With n=16 treat the p-value as a sanity check, not a claim.

## Dataset integrity: why dataset_v2 exists

dataset_v1's *videos* frequently did not contain the labelled defect: Playwright
only receives a frame when the page repaints, and its recorder finalises
asynchronously on close — so a static broken state (vanished app, off-screen
app) emitted a single defect frame that often lost the close race. Every
`flyaway_offset` clip and 3/5 `vanish_invisible` clips were corrupted this way,
while animated bugs (`flicker`) always survived. The screenshot-level pixelmatch
gate could not see this because it never decoded the video.

Three fixes in `latest/tests/poc-bug-injection.spec.ts` + `poc/gate_videos.py`:
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

### Requirements

Python 3 with `google-generativeai`, `pillow`, `requests`, `numpy`, and
`imageio-ffmpeg` (all present in this environment). You must supply your own
Gemini API key via the environment — for security it is never stored in the
repo.
