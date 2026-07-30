#!/usr/bin/env python3
"""Stage 3 — the paired, config-driven condition trial (EXP002).

Every manifest clip is evaluated under EVERY condition listed in the conditions
config, so the comparison is a paired design (Cochran's Q / McNemar-testable)
rather than independent samples. The condition grammar itself lives in
`conditions.py`; the condition set, repeats and generation config are read from
a frozen spec (default experiments/exp002/config.frozen.json), so adding or
removing a condition is a config edit, not a code change.

Fairness rules enforced here:
  - One model, resolved once (cheapest available), frozen for all calls.
  - The per-clip ground-truth `expectation` NEVER enters any prompt — the
    "expected appearance" text is generic and identical for every clip.
  - Keyframe selection reads only pixels / DOM, never the label.
  - Conditions (and repeats) are interleaved per clip, so if quota dies mid-run
    every arm loses the same clips and pairing holds.
  - Every response is cached in the raw dir; re-running resumes.

The three prompt templates below are integrity-pinned by SHA-256 in the frozen
config, so their text must not be edited while EXP002 is being collected.

Usage:
  python3 pipeline/run_trial.py --dry-run            # build all payloads, no API calls
  python3 pipeline/run_trial.py --dry-run --limit 2  # smoke test payloads on 2 clips
  python3 pipeline/run_trial.py --limit 2            # real calls on 2 clips (needs key)
  python3 pipeline/run_trial.py                       # the full trial
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPELINE_DIR))

import conditions as cond  # noqa: E402
import keyframes as kf  # noqa: E402
import paths  # noqa: E402
import vlm_client as vc  # noqa: E402
from paths import REPO_ROOT  # noqa: E402

EXPERIMENT_ID = "exp002"
DEFAULT_CONFIG = paths.frozen_config(EXPERIMENT_ID)
RESULTS_DIR = paths.results_dir(EXPERIMENT_ID)
RAW_DIR = paths.raw_dir(EXPERIMENT_ID)

BASELINE_FRAMES = 6
FRAME_WIDTH = 640  # downscale width for all conditions (fairness + cost)

# Window used to summarise DOM activity around the selected transition FOR THE
# PROMPT. Deliberately a separate knob from keyframes.DOM_FUSE_WINDOW_MS (which
# governs A3's selection-time fusion) — the frozen config lists the two as
# independent parameters — but both are 1200ms so selection and the prompt's DOM
# summary reason over the same neighbourhood.
#
# DOM trace timestamps start at init-script install (~navigation), the video at
# context creation; the offset is the page-load delay, well under a second.
# A generous window keeps the correlation honest despite that jitter.
DOM_WINDOW_MS = 1200

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


# --- payload construction ---------------------------------------------------

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


def analyze_clip(case: dict) -> dict:
    """Decode the clip once and precompute every selector's frames + signals,
    so each condition just picks the pieces it needs."""
    frames, fps = kf.decode(str(REPO_ROOT / case["video_path"]))

    trace_rel = case.get("dom_trace_path")
    trace = None
    if trace_rel and (REPO_ROOT / trace_rel).exists():
        trace = paths.read_json(REPO_ROOT / trace_rel)

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
    a_level, b_level = cond.split(code)
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


# --- execution --------------------------------------------------------------

@dataclass
class Session:
    """Everything the per-clip loop needs to know about *how* to run.

    In a dry run the model handles stay None and no request is ever built, so
    the payload path can be exercised without an API key.
    """

    dry_run: bool
    generation_config: dict | None = None
    genai: object | None = None
    model_name: str | None = None

    @property
    def label(self) -> str:
        return "DRY-RUN" if self.dry_run else str(self.model_name)


def call_model(session: Session, payload: dict) -> dict:
    parts = [payload["prompt"]] + [vc.frame_to_part(f) for f in payload["frames"]]
    t0 = time.time()
    result = vc.call_json(session.genai, session.model_name, parts,
                          generation_config=session.generation_config)
    result["_latency_s"] = round(time.time() - t0, 2)
    return result


def save_dry_record(case: dict, code: str, rep: int, payload: dict) -> None:
    """A dry run records the prompt it *would* have sent, so payloads are
    reviewable (and prompt hashes verifiable) before any quota is spent."""
    record = {
        "id": case["id"], "condition": code, "repeat": rep,
        "dry_run": True, "prompt": payload["prompt"],
        "n_images": len(payload["frames"]), "meta": payload["meta"],
    }
    paths.write_json(RAW_DIR / f"DRY__{case['id']}__{code}__r{rep}.json", record)
    print(f"[dry ] {case['id']:30s} {code} r{rep} "
          f"imgs={record['n_images']} sel={payload['meta']['selection']}")


def save_call_record(out: Path, case: dict, code: str, rep: int,
                     payload: dict, session: Session) -> None:
    resp = call_model(session, payload)
    record = {
        "id": case["id"], "condition": code, "repeat": rep,
        "model": session.model_name,
        "verdict": resp.get("verdict"),
        "reason": resp.get("reason"),
        "defect_type": resp.get("defect_type"),
        "raw": resp.get("_raw"),
        "usage": resp.get("_usage"),
        "latency_s": resp.get("_latency_s"),
        "meta": payload["meta"],
    }
    paths.write_json(out, record)
    print(f"[call] {case['id']:30s} {code} r{rep} -> {record['verdict']}")


def process_clip(case: dict, cfg: cond.TrialConfig, session: Session) -> tuple[int, int]:
    """Run every (repeat, condition) cell for one clip. Returns (done, skipped).

    The clip is decoded at most once, and only if at least one cell is actually
    missing from the cache — so resuming a finished run costs no video decoding.
    """
    done = skipped = 0
    ctx = None
    for rep in range(1, cfg.repeats + 1):
        for code in cfg.conditions:  # interleaved by construction
            out = RAW_DIR / f"{case['id']}__{code}__r{rep}.json"
            if out.exists():
                skipped += 1
                continue
            if ctx is None:
                ctx = analyze_clip(case)
            payload = build_condition_payload(ctx, code)

            if session.dry_run:
                save_dry_record(case, code, rep, payload)
                continue

            save_call_record(out, case, code, rep, payload, session)
            done += 1
    return done, skipped


# --- entry point ------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                    help="conditions config (default: EXP002 frozen spec)")
    ap.add_argument("--dry-run", action="store_true",
                    help="build payloads and save prompts, make no API calls")
    ap.add_argument("--limit", type=int, default=None,
                    help="only the first N clips (quota probe / smoke test)")
    ap.add_argument("--repeats", type=int, default=None,
                    help="override the repeats count from the config")
    ap.add_argument("--conditions", type=str, default=None,
                    help="comma-separated condition codes overriding the config")
    return ap.parse_args()


def resolve_config(args: argparse.Namespace) -> cond.TrialConfig:
    """The frozen spec, with the CLI overrides applied (and re-validated)."""
    cfg = cond.load(args.config)
    if args.conditions:
        cfg = cfg.with_conditions(
            [c.strip() for c in args.conditions.split(",") if c.strip()])
    if args.repeats is not None:
        cfg = replace(cfg, repeats=args.repeats)
    return cfg


def save_trial_config(cfg: cond.TrialConfig, session: Session, manifest: dict) -> None:
    """Record the exact apparatus this run used, next to its results."""
    paths.write_json(RESULTS_DIR / "trial-config.json", {
        "model": session.model_name,
        "conditions": cfg.conditions,
        "repeats": cfg.repeats,
        "generation_config": cfg.generation_config,
        "baseline_frames": BASELINE_FRAMES,
        "frame_width": FRAME_WIDTH,
        "dom_window_ms": DOM_WINDOW_MS,
        "config_source": cfg.source,
        "manifest_total": manifest["total"],
    })


def main() -> None:
    args = parse_args()
    cfg = resolve_config(args)

    manifest = paths.read_json(paths.MANIFEST_JSON)
    cases = manifest["cases"][: args.limit] if args.limit else manifest["cases"]
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    session = Session(dry_run=args.dry_run, generation_config=cfg.generation_config)
    if not session.dry_run:
        session.genai, session.model_name = vc.connect()
        save_trial_config(cfg, session, manifest)

    print(f"Conditions: {cfg.conditions} x {cfg.repeats} repeat(s) | "
          f"{len(cases)} clip(s) | {session.label}")

    done = skipped = 0
    for case in cases:
        d, s = process_clip(case, cfg, session)
        done += d
        skipped += s

    print(f"\n{'Dry run' if args.dry_run else 'Trial'} finished: "
          f"{done} new calls, {skipped} cached. Results in {RAW_DIR}")
    if not args.dry_run:
        print("Score with: python3 pipeline/score_trial.py")


if __name__ == "__main__":
    main()
