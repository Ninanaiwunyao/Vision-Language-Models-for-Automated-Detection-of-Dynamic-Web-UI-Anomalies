#!/usr/bin/env python3
"""Stage 3 — the paired, config-driven condition trial (EXP002).

Every manifest clip is evaluated under EVERY condition listed in the conditions
config, so the comparison is a paired design (Cochran's Q / McNemar-testable)
rather than independent samples.

Conditions are named by a 2-factor code, e.g. A3B1:

  Factor A — which frames are sent to the model:
    A0  uniform sampling (no selection)
    A1  pixel-diff-only keyframe selection
    A3  pixel + DOM fused selection (DOM influences which frames are sent)
  Factor B — the prompt:
    B0  naive prompt
    B1  dual-path decision prompt (DOM evidence text lives here, under B)

The condition set, repeats, and generation config are read from a config file
(default experiments/exp002/conditions.draft.json), so adding or removing a
condition is a config edit, not a code change.

Fairness rules enforced here:
  - One model, resolved once (cheapest available), frozen for all calls.
  - The per-clip ground-truth `expectation` NEVER enters any prompt — the
    "expected appearance" text is generic and identical for every clip.
  - Keyframe selection reads only pixels / DOM, never the label.
  - Conditions (and repeats) are interleaved per clip, so if quota dies mid-run
    every arm loses the same clips and pairing holds.
  - Every response is cached in the raw dir; re-running resumes.

Usage:
  python3 poc/run_trial.py --dry-run            # build all payloads, no API calls
  python3 poc/run_trial.py --dry-run --limit 2  # smoke test payloads on 2 clips
  python3 poc/run_trial.py --limit 2            # real calls on 2 clips (needs key)
  python3 poc/run_trial.py                       # the full trial
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

POC_DIR = Path(__file__).resolve().parent
REPO_ROOT = POC_DIR.parent
sys.path.insert(0, str(POC_DIR))

import keyframes as kf  # noqa: E402
import vlm_client as vc  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "experiments" / "exp002" / "conditions.draft.json"
RESULTS_DIR = POC_DIR / "results" / "exp002"
RAW_DIR = RESULTS_DIR / "raw"

BASELINE_FRAMES = 6
FRAME_WIDTH = 640  # downscale width for all conditions (fairness + cost)

# DOM trace timestamps start at init-script install (~navigation), the video at
# context creation; the offset is the page-load delay, well under a second.
# A generous window keeps the correlation honest despite that jitter.
DOM_WINDOW_MS = 1200

CONDITION_RE = re.compile(r"^(A\d)(B\d)$")

# B0 "naive" prompt. Deliberately says nothing about HOW the frames were chosen,
# so it is a clean control for any factor-A level: pairing it with A3 (fused
# selection) must not leak the selection method into the prompt. (EXP001's
# baseline said "sampled uniformly"; that wording is dropped for EXP002 because
# it would be a false claim under A3B0.)
BASELINE_PROMPT = (
    "You are shown {k} frames from a screen recording of a web application "
    "session. Decide whether the recording shows a visual bug or rendering "
    "defect. Respond with ONLY a JSON object, no prose, no code fence: "
    '{{"verdict": "anomaly" | "normal", "reason": "<one sentence>"}}'
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


def summarize_dom(trace: list | None, transition_ms: float) -> dict:
    """Compress the mutation trace into the few numbers the prompt fuses.
    Returns an 'unavailable' marker for pre-v2 datasets with no trace."""
    if not trace:
        return {"available": False}
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


# --- config ----------------------------------------------------------------

def validate_conditions(codes: list[str]) -> None:
    """Reject any code the pipeline cannot actually run, with a clear reason."""
    if not codes:
        raise SystemExit("No conditions to run")
    for code in codes:
        m = CONDITION_RE.match(code)
        if not m:
            raise SystemExit(f"Malformed condition code '{code}' (expected AxBy)")
        a, b = m.group(1), m.group(2)
        if a not in ("A0", "A1", "A3"):
            raise SystemExit(f"{code}: frame selector {a} not implemented")
        if b not in ("B0", "B1"):
            raise SystemExit(f"{code}: prompt variant {b} not implemented")
        if b == "B1" and a == "A0":
            raise SystemExit(
                f"{code}: B1 (dual-path) needs a transition-based selector; "
                f"A0 (uniform) has no transition to describe"
            )


def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"Conditions config not found: {path}")
    cfg = json.loads(path.read_text())
    conditions = cfg.get("conditions")
    if not conditions:
        raise SystemExit(f"No 'conditions' list in {path}")
    validate_conditions(conditions)
    # Strip comment keys from the generation config before it reaches the SDK.
    gen = {k: v for k, v in cfg.get("generation_config", {}).items()
           if not k.startswith("_")}
    return {"conditions": conditions, "repeats": int(cfg.get("repeats", 1)),
            "generation_config": gen or None, "source": str(path)}


# --- per-clip analysis (decode once) ---------------------------------------

def analyze_clip(case: dict) -> dict:
    """Decode the clip once and precompute every selector's frames + signals,
    so each condition just picks the pieces it needs."""
    frames, fps = kf.decode(str(REPO_ROOT / case["video_path"]))

    trace_rel = case.get("dom_trace_path")
    trace = None
    if trace_rel and (REPO_ROOT / trace_rel).exists():
        trace = json.loads((REPO_ROOT / trace_rel).read_text())

    uni = [frames[i] for i in kf.uniform_indices(len(frames), BASELINE_FRAMES)]
    return {
        "n_frames": len(frames),
        "uniform": uni,
        "A1": kf.select_transition(frames, fps),
        "A3": kf.select_fused(frames, fps, trace),
        "trace": trace,
    }


def frames_for_a(ctx: dict, a_level: str) -> list:
    if a_level == "A0":
        return ctx["uniform"]
    sel = ctx[a_level]  # A1 or A3
    return [sel["before"], sel["after"], sel["final"]]


def build_condition_payload(ctx: dict, code: str) -> dict:
    a_level, b_level = CONDITION_RE.match(code).groups()
    frames = frames_for_a(ctx, a_level)

    if b_level == "B0":
        prompt = BASELINE_PROMPT.format(k=len(frames))
        meta = {"selection": a_level, "n_images": len(frames)}
    else:  # B1 — evidence comes from the selection that chose these frames
        sel = ctx[a_level]
        dom = summarize_dom(ctx["trace"], sel["time_s"] * 1000)
        dom_text = (json.dumps(dom, indent=2) if dom["available"]
                    else "No DOM trace was recorded for this clip.")
        prompt = OURS_PROMPT.format(
            expected=EXPECTED_APPEARANCE, t=sel["time_s"],
            signals=json.dumps(sel["signal"], indent=2), dom=dom_text,
        )
        meta = {"selection": a_level, "n_images": len(frames),
                "transition_time_s": sel["time_s"], "dom": dom}

    return {"prompt": prompt, "frames": [resize(f) for f in frames], "meta": meta}


def run_condition(genai, model_name, payload, gen_config) -> dict:
    parts = [payload["prompt"]] + [vc.frame_to_part(f) for f in payload["frames"]]
    t0 = time.time()
    result = vc.call_json(genai, model_name, parts, generation_config=gen_config)
    result["_latency_s"] = round(time.time() - t0, 2)
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                    help="conditions config (default: EXP002 draft)")
    ap.add_argument("--dry-run", action="store_true",
                    help="build payloads and save prompts, make no API calls")
    ap.add_argument("--limit", type=int, default=None,
                    help="only the first N clips (quota probe / smoke test)")
    ap.add_argument("--repeats", type=int, default=None,
                    help="override the repeats count from the config")
    ap.add_argument("--conditions", type=str, default=None,
                    help="comma-separated condition codes overriding the config")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.conditions:
        override = [c.strip() for c in args.conditions.split(",") if c.strip()]
        validate_conditions(override)
        cfg["conditions"] = override
    conditions = cfg["conditions"]
    repeats = args.repeats if args.repeats is not None else cfg["repeats"]
    gen_config = cfg["generation_config"]

    manifest = json.loads((POC_DIR / "manifest.json").read_text())
    cases = manifest["cases"][: args.limit] if args.limit else manifest["cases"]
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    genai = model_name = None
    if not args.dry_run:
        genai, model_name = vc.connect()
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (RESULTS_DIR / "trial-config.json").write_text(json.dumps({
            "model": model_name,
            "conditions": conditions,
            "repeats": repeats,
            "generation_config": gen_config,
            "baseline_frames": BASELINE_FRAMES,
            "frame_width": FRAME_WIDTH,
            "dom_window_ms": DOM_WINDOW_MS,
            "config_source": cfg["source"],
            "manifest_total": manifest["total"],
        }, indent=2) + "\n")

    print(f"Conditions: {conditions} x {repeats} repeat(s) | "
          f"{len(cases)} clip(s) | {'DRY-RUN' if args.dry_run else model_name}")

    done = skipped = 0
    for case in cases:
        ctx = None  # decode lazily, once per clip
        for rep in range(1, repeats + 1):
            for code in conditions:  # interleaved by construction
                out = RAW_DIR / f"{case['id']}__{code}__r{rep}.json"
                if out.exists():
                    skipped += 1
                    continue
                if ctx is None:
                    ctx = analyze_clip(case)
                payload = build_condition_payload(ctx, code)

                if args.dry_run:
                    record = {
                        "id": case["id"], "condition": code, "repeat": rep,
                        "dry_run": True, "prompt": payload["prompt"],
                        "n_images": len(payload["frames"]), "meta": payload["meta"],
                    }
                    (RAW_DIR / f"DRY__{case['id']}__{code}__r{rep}.json").write_text(
                        json.dumps(record, indent=2) + "\n")
                    print(f"[dry ] {case['id']:30s} {code} r{rep} "
                          f"imgs={record['n_images']} sel={payload['meta']['selection']}")
                    continue

                resp = run_condition(genai, model_name, payload, gen_config)
                record = {
                    "id": case["id"], "condition": code, "repeat": rep,
                    "model": model_name,
                    "verdict": resp.get("verdict"),
                    "reason": resp.get("reason"),
                    "defect_type": resp.get("defect_type"),
                    "raw": resp.get("_raw"),
                    "usage": resp.get("_usage"),
                    "latency_s": resp.get("_latency_s"),
                    "meta": payload["meta"],
                }
                out.write_text(json.dumps(record, indent=2) + "\n")
                done += 1
                print(f"[call] {case['id']:30s} {code} r{rep} -> {record['verdict']}")

    print(f"\n{'Dry run' if args.dry_run else 'Trial'} finished: "
          f"{done} new calls, {skipped} cached. Results in {RAW_DIR}")
    if not args.dry_run:
        print("Score with: python3 poc/score_trial.py")


if __name__ == "__main__":
    main()
