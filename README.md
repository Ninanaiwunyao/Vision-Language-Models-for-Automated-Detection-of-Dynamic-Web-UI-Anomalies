# Vision-Language Models for Automated Testing of Dynamic Interactive UI and Animation Anomalies

## Project Overview

This project proposes an AI-driven, dual-path visual regression testing framework designed to detect dynamic UI anomalies (e.g., layout shifts, z-index conflicts, and animation stuttering) in modern web applications.

Traditional pixel-based testing tools often suffer from high false-positive rates (exceeding 70%) when dealing with dynamic content. Conversely, blindly feeding entire gameplay or web interaction videos to Vision-Language Models (VLMs) yields near-chance accuracy and high token costs. This project addresses these pain points by introducing an **Intelligent Keyframe Selection** preprocessing mechanism and a **Dual-path Architecture**.

## Architecture Design

The framework consists of two main paths to balance precision and efficiency:

1. **Path B (Structural/Visual Path - The Alarm):** Utilizes DOM Mutation Observers and `pixelmatch` to continuously monitor the UI state. It acts as an efficient anomaly filter to capture highly significant layout divergences.
2. **Path A (Semantic Path - The Brain):** Employs Multimodal LLMs (e.g., Gemini 3 Flash, Qwen2.5-VL) as reasoning engines. It is triggered only when Path B flags a structural change, analyzing whether the change is an acceptable responsive UI update or an actual defect.

## Repository Structure & Dataset

To eliminate non-deterministic anti-bot mechanisms (e.g., CAPTCHAs) and network latency, this project utilizes the globally recognized **TodoMVC** (React, Vue, VanillaJS) as the benchmark subject application.

- `/dataset_v1/`: The curated Proof-of-Concept (PoC) dataset containing 40 high-contrast synthetic UI anomaly videos (.webm). These were generated entirely automatically via Playwright by mutating CSS/DOM parameters, inspired by the _GUI-Robust (KDD 2026)_ semi-automated generation paradigm. All 40 videos have successfully passed the automated `pixelmatch` quality gate.
- `/tests/`: Contains the Playwright data-driven automated fault injection scripts (`poc-bug-injection.spec.ts`).
- `/test-results/`: Ephemeral directory for local test execution output (ignored in Git).
- `worklog.md`: Development log tracking daily experiments, architectural decisions, and hardware (vLab) migration progress.

## Quick Start

### 1. Generate Synthetic UI Anomalies

Ensure you have Node.js and Playwright installed:

```bash
npm install
npx playwright install chromium
Run the automated fault injection pipeline (generates buggy videos in test-results/):
npx playwright test poc-bug-injection.spec.ts
```

### 2. Zero-shot VLM Evaluation (Running on vLab GPU)

Note: Due to the unshared filesystem between vLab CPU and GPU instances, curated datasets must be pulled via Git
.
Check out this repository in the vLab GPU environment.
Ensure you have the required API keys (e.g., Google AI Studio for Gemini 3 Flash) or local models (Qwen2.5-VL-7B) configured
.
Run the evaluation script targeting the videos inside dataset_v1/.

## Honour Code & GenAI Disclosure

In accordance with the university's Holistic Assessment and GenAI usage guidelines, Generative AI tools (e.g., Gemini, Claude) are utilized as collaborative engineering partners in this project
. Specific AI contributions—such as architectural consultation, Playwright scripting assistance, and report structuring—are explicitly documented within each Git commit message under the GENAI ASSISTANCE STATEMENT section
. All AI-generated outputs have been manually reviewed, tested, and refactored by the author to ensure full project ownership
