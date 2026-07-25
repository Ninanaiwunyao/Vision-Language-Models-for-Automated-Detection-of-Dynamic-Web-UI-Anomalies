#!/usr/bin/env python3
"""Stage 3 — the paired two-condition trial (the heart of the PoC).

Every manifest clip is evaluated under BOTH conditions, so the comparison is a
paired design (McNemar-testable) rather than two independent samples:

  baseline  Uniform sampling + naive prompt. K frames spread evenly over the
            clip, "is there a visual bug?". This replicates the TempGlitch-style
            brute-force approach we argue against.

  ours      Smart keyframes + dual-path decision prompt. The pixel change-point
            detector picks the strongest sustained transition (2 frames:
            before/after), the DOM trace is correlated against that moment, and
            the prompt fuses: a generic expected-appearance reference, the
            structural pixel signals, and the DOM evidence.

Fairness rules enforced here:
  - One model, resolved once (cheapest available), frozen for all calls.
  - The per-clip ground-truth `expectation` NEVER enters any prompt — the
    "expected appearance" text in ours is generic and identical for every clip.
  - Keyframe selection reads only pixels/DOM, never the label.
  - Conditions are interleaved (clip1-baseline, clip1-ours, clip2-baseline, …)
    so if quota dies mid-run, both arms lose the same clips and pairing holds.
  - Every response is cached in poc/results/raw/; re-running resumes.

Usage:
  python3 poc/run_trial.py --dry-run    # build all payloads, no API calls
  python3 poc/run_trial.py              # the real thing (needs GOOGLE_API_KEY)
  python3 poc/run_trial.py --limit 4    # first N clips only (quota probe)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

POC_DIR = Path(__file__).resolve().parent
REPO_ROOT = POC_DIR.parent
sys.path.insert(0, str(POC_DIR))

import keyframes as kf  # noqa: E402
import vlm_client as vc  # noqa: E402

RESULTS_DIR = POC_DIR / "results"
RAW_DIR = RESULTS_DIR / "raw"

BASELINE_FRAMES = 6
FRAME_WIDTH = 640  # downscale width for both conditions (fairness + cost)

# DOM trace timestamps start at init-script install (~navigation), the video at
# context creation; the offset is the page-load delay, well under a second.
# A generous window keeps the correlation honest despite that jitter.
DOM_WINDOW_MS = 1200

BASELINE_PROMPT = (
    "You are shown {k} frames sampled uniformly from a screen recording of a "
    "web application session. Decide whether the recording shows a visual bug "
    "or rendering defect. Respond with ONLY a JSON object, no prose, no code "
    'fence: {{"verdict": "anomaly" | "normal", "reason": "<one sentence>"}}'
)

# Generic expected-appearance reference — identical for every clip; contains
# nothing about which defect (if any) was injected into this particular clip.
EXPECTED_APPEARANCE = (
    "The application under test is a standard TodoMVC to-do list: a white card "
    "on a light-grey page, a large light-red 'todos' heading, an input reading "
    "'What needs to be done?', list rows with round toggle checkboxes "
    "(completed items greyed with strikethrough), and a footer with item count "
    "and All/Active/Completed filters. Text is crisp and upright, the card is "
    "centred and fully visible, and colours are white/grey with red accents. "
    "A tiny grey dot at the bottom-right corner is a recording indicator from "
    "the test harness — ignore it."
)

OURS_PROMPT = """You are a UI regression analyst. Decide whether a screen \
recording of a web app contains a RENDERING DEFECT, using the evidence below.

EXPECTED APPEARANCE OF THE APP:
{expected}

EVIDENCE — an automated monitor found the strongest sustained visual \
transition in the recording at t={t}s:
- Image 1: the frame BEFORE that transition.
- Image 2: the frame AFTER that transition.
- Image 3: the FINAL frame of the recording (the settled end state).

STRUCTURAL SIGNALS from the monitor:
{signals}

DOM ACTIVITY (from a MutationObserver on the page; CSS-only rendering changes \
produce no DOM mutations):
{dom}

DECISION RULES:
1. Content changes (todos added/completed/deleted, filter switched) are \
NORMAL user actions; they coincide with DOM mutations and the app still \
matches the expected appearance afterwards.
2. A visual transition WITHOUT corresponding DOM activity is suspicious: it \
suggests a pure-rendering change the application logic did not intend.
3. It is a DEFECT if the AFTER state or the FINAL state deviates from the \
expected appearance: geometric distortion, blurred/unreadable text, the app \
disappearing or shifting far off-position, violent flicker or unexpected \
colour swaps, or a whole section missing.

Respond with ONLY a JSON object, no prose, no code fence:
{{"verdict": "anomaly" | "normal", "reason": "<one sentence>", \
"defect_type": <one of "skew_distortion","blur_unreadable","vanish_invisible",\
"flyaway_offset","flicker_seizure","displaynone_collapse" or null>}}"""


def resize(frame, width=FRAME_WIDTH):
    import numpy as np
    from PIL import Image

    img = Image.fromarray(frame.astype("uint8"), mode="RGB")
    h = int(round(img.height * width / img.width))
    return np.asarray(img.resize((width, h), Image.BILINEAR))


def summarize_dom(trace_path: Path | None, transition_ms: float) -> dict:
    """Compress the mutation trace into the few numbers the prompt fuses.
    Returns an 'unavailable' marker for pre-v2 datasets with no trace."""
    if not trace_path or not trace_path.exists():
        return {"available": False}
    trace = json.loads(trace_path.read_text())
    lo, hi = transition_ms - DOM_WINDOW_MS, transition_ms + DOM_WINDOW_MS
    near = [m for m in trace if lo <= m["t"] <= hi]
    types: dict[str, int] = {}
    for m in near:
        key = f"{m['type']}:{m.get('tag') or '?'}"
        types[key] = types.get(key, 0) + 1
    return {
        "available": True,
        "total_mutations": len(trace),
        "mutations_near_transition": len(near),
        "near_breakdown": dict(sorted(types.items(), key=lambda kv: -kv[1])[:6]),
    }


def build_payloads(case: dict) -> dict:
    """Decode once, then derive both conditions' inputs from the same frames."""
    frames, fps = kf.decode(str(REPO_ROOT / case["video_path"]))

    uni = [frames[i] for i in kf.uniform_indices(len(frames), BASELINE_FRAMES)]

    sel = kf.select_transition(frames, fps)
    trace_rel = case.get("dom_trace_path")
    dom = summarize_dom(REPO_ROOT / trace_rel if trace_rel else None,
                        sel["time_s"] * 1000)

    dom_text = (
        json.dumps(dom, indent=2)
        if dom["available"]
        else "No DOM trace was recorded for this clip."
    )
    ours_prompt = OURS_PROMPT.format(
        expected=EXPECTED_APPEARANCE,
        t=sel["time_s"],
        signals=json.dumps(sel["signal"], indent=2),
        dom=dom_text,
    )
    return {
        "baseline": {
            "prompt": BASELINE_PROMPT.format(k=len(uni)),
            "frames": [resize(f) for f in uni],
        },
        "ours": {
            "prompt": ours_prompt,
            "frames": [resize(sel["before"]), resize(sel["after"]),
                       resize(sel["final"])],
        },
        "meta": {
            "transition_time_s": sel["time_s"],
            "signal": sel["signal"],
            "dom": dom,
            "n_frames_decoded": len(frames),
        },
    }


def run_condition(genai, model_name, payload: dict) -> dict:
    parts = [payload["prompt"]] + [vc.frame_to_part(f) for f in payload["frames"]]
    t0 = time.time()
    result = vc.call_json(genai, model_name, parts)
    result["_latency_s"] = round(time.time() - t0, 2)
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="build payloads and save prompts, make no API calls")
    ap.add_argument("--limit", type=int, default=None,
                    help="only the first N clips (quota probe)")
    args = ap.parse_args()

    manifest = json.loads((POC_DIR / "manifest.json").read_text())
    cases = manifest["cases"][: args.limit] if args.limit else manifest["cases"]
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    genai = model_name = None
    if not args.dry_run:
        genai, model_name = vc.connect()
        (RESULTS_DIR / "trial-config.json").write_text(json.dumps({
            "model": model_name,
            "baseline_frames": BASELINE_FRAMES,
            "frame_width": FRAME_WIDTH,
            "dom_window_ms": DOM_WINDOW_MS,
            "manifest_total": manifest["total"],
        }, indent=2) + "\n")

    done = skipped = 0
    for case in cases:
        payloads = None
        for cond in ("baseline", "ours"):  # interleaved by construction
            out = RAW_DIR / f"{case['id']}__{cond}.json"
            if out.exists():
                skipped += 1
                continue
            if payloads is None:
                payloads = build_payloads(case)  # decode lazily, once per clip

            if args.dry_run:
                record = {
                    "id": case["id"], "condition": cond, "dry_run": True,
                    "prompt": payloads[cond]["prompt"],
                    "n_images": len(payloads[cond]["frames"]),
                    "meta": payloads["meta"],
                }
                (RAW_DIR / f"DRY__{case['id']}__{cond}.json").write_text(
                    json.dumps(record, indent=2) + "\n")
                print(f"[dry ] {case['id']:30s} {cond:8s} "
                      f"imgs={record['n_images']} t={payloads['meta']['transition_time_s']}s "
                      f"dom_near={payloads['meta']['dom'].get('mutations_near_transition', '-')}")
                continue

            resp = run_condition(genai, model_name, payloads[cond])
            record = {
                "id": case["id"], "condition": cond, "model": model_name,
                "verdict": resp.get("verdict"),
                "reason": resp.get("reason"),
                "defect_type": resp.get("defect_type"),
                "raw": resp.get("_raw"),
                "latency_s": resp.get("_latency_s"),
                "meta": payloads["meta"],
            }
            out.write_text(json.dumps(record, indent=2) + "\n")
            done += 1
            print(f"[call] {case['id']:30s} {cond:8s} -> {record['verdict']}")

    print(f"\n{'Dry run' if args.dry_run else 'Trial'} finished: "
          f"{done} new calls, {skipped} cached. Results in {RAW_DIR}")
    if not args.dry_run:
        print("Score with: python3 poc/score_trial.py")


if __name__ == "__main__":
    main()
