# Vision-Language Models for Automated Testing of Dynamic Interactive UI and Animation Anomalies

## Project Overview

This project proposes an AI-driven, dual-path visual regression testing framework designed to detect dynamic UI anomalies (e.g., layout shifts, z-index conflicts, and animation stuttering) in modern web applications.

Traditional pixel-based testing tools often suffer from high false-positive rates (exceeding 70%) when dealing with dynamic content. Conversely, blindly feeding entire gameplay or web interaction videos to Vision-Language Models (VLMs) yields near-chance accuracy and high token costs. This project addresses these pain points by introducing an **Intelligent Keyframe Selection** preprocessing mechanism and a **Dual-path Architecture**.

## Architecture Design

The framework consists of two main paths to balance precision and efficiency:

1. **Path B (Structural/Visual Path - The Alarm):** Utilizes DOM Mutation Observers and `pixelmatch` to continuously monitor the UI state. It acts as an efficient anomaly filter to capture highly significant layout divergences.
2. **Path A (Semantic Path - The Brain):** Employs Multimodal LLMs (e.g., Gemini 3 Flash, Qwen2.5-VL) as reasoning engines. It is triggered only when Path B flags a structural change, analyzing whether the change is an acceptable responsive UI update or an actual defect.

## Repository Structure

To eliminate non-deterministic anti-bot mechanisms (e.g., CAPTCHAs) and network latency, this project uses the widely recognised **TodoMVC** (React, Vue, VanillaJS) as the benchmark subject application.

The tree separates four things that have genuinely different lifecycles — code can be refactored freely, ground truth is regenerable, results are evidence, and the frozen specs must not move:

| path | contents |
|---|---|
| `generator/` | Playwright fault-injection suite that produces the dataset (`tests/bug-injection.spec.ts`) plus its quality gate |
| `pipeline/` | the Python evaluation pipeline: manifest, keyframe selection, VLM client, trial runner, scoring and statistics |
| `data/` | `manifest.json` / `manifest.csv` — dataset-derived ground truth, one record per clip |
| `results/<exp>/` | per-experiment evidence: the `raw/` response cache, `summary.json`, `trial-config.json` |
| `experiments/<exp>/` | `config.frozen.json` — the frozen specification each run must satisfy |
| `docs/` | the dataset standard and other written specifications |
| `dataset_v3/` | 120 recorded clips (.webm + keyframes + DOM traces). **Not in git** — see below |
| `worklog.md` | development log: experiments, architectural decisions, and the GenAI appendix |

Datasets are excluded from version control because GitLab caps project storage at ~100MB. They are fully regenerable from the pinned generator plus the `RNG_SEED` recorded in `experiments/*/config.frozen.json`, so reproducibility does not depend on shipping the binaries.

## Quick Start

### 1. Generate the synthetic anomaly dataset

Requires Node.js and Playwright:

```bash
cd generator && npm install && npx playwright install chromium
```

```bash
cd generator && npx playwright test bug-injection.spec.ts
```

Then verify every labelled defect is actually visible in the recording, not just in the screenshots:

```bash
python3 pipeline/gate_videos.py dataset_v3
```

### 2. VLM evaluation

```bash
export GOOGLE_API_KEY=...            # Google AI Studio key
export GEMINI_MODEL=...              # the model id pinned by the frozen config
```

```bash
python3 pipeline/build_manifest.py dataset_v3 && python3 pipeline/smoke_test.py
```

Both gates must be green before the trial: otherwise an accuracy number cannot be attributed to the method rather than to broken plumbing.

```bash
python3 pipeline/run_trial.py && python3 pipeline/score_trial.py
```

`run_trial.py` caches each response under `results/<exp>/raw/`, so an interrupted run resumes instead of re-spending quota. See [`pipeline/README.md`](pipeline/README.md) for the full stage-by-stage description.

## Honour Code & GenAI Disclosure

In accordance with the university's Holistic Assessment and GenAI usage guidelines, Generative AI tools (e.g., Gemini, Claude) are utilized as collaborative engineering partners in this project
. Specific AI contributions—such as architectural consultation, Playwright scripting assistance, and report structuring—are explicitly documented within each Git commit message under the GENAI ASSISTANCE STATEMENT section
. All AI-generated outputs have been manually reviewed, tested, and refactored by the author to ensure full project ownership
