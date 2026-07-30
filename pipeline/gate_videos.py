#!/usr/bin/env python3
"""Video-level quality gate — the check dataset_v1 was missing.

The Playwright gate compares normal.png vs buggy.png: two screenshots taken
in-process. Those genuinely contain the defect, which is why all 40 v1 cases
passed. But the artifact we actually feed the VLM is the *video*, and the gate
never looked at it. Playwright truncates the tail of a recording when the
context closes, and in v1 the defect lived only in that tail — so clips labelled
"anomaly" showed a perfectly normal app. Every flyaway_offset clip and 3/5
vanish_invisible clips were corrupted this way.

This gate closes that hole. For each buggy case it decodes the video, takes the
final frame, crops it to the exact region the screenshots cover (the clip
recorded in gate-report.json), and asks a simple question:

    does the last frame look like buggy.png, or like normal.png?

If it resembles normal.png, the defect never made it into the video and the
case is REJECTED regardless of what the screenshot gate said.

Control cases have buggy.png == normal.png (nothing injected), so there is no
defect to verify; they pass automatically as long as the video decodes.

Usage:
  python3 pipeline/gate_videos.py <dataset-dir>          # report only
  python3 pipeline/gate_videos.py <dataset-dir> --prune  # also delete failed cases
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image

PIPELINE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPELINE_DIR))

import paths  # noqa: E402

# pixelmatch's YIQ colour-delta constants. TodoMVC's white card (#fff) sits on a
# #f5f5f5 page — only ~10 intensity levels apart — so a naive RGB threshold is
# literally blind to the app vanishing. This is the same metric the Playwright
# gate uses, keeping the two gates consistent.
MAX_DELTA = 35215.0
PIXELMATCH_THRESHOLD = 0.03

# The last frame must be at least this much closer to buggy.png than to
# normal.png. A margin (rather than a bare <) avoids passing a case on noise.
DECISION_MARGIN = 1.25


def color_delta_mask(a: np.ndarray, b: np.ndarray, threshold: float = PIXELMATCH_THRESHOLD) -> np.ndarray:
    """Boolean mask of perceptually different pixels (pixelmatch's YIQ metric)."""
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    dr = a[..., 0] - b[..., 0]
    dg = a[..., 1] - b[..., 1]
    db = a[..., 2] - b[..., 2]
    y = dr * 0.29889531 + dg * 0.58662247 + db * 0.11448223
    i = dr * 0.59597799 - dg * 0.27417610 - db * 0.32180189
    q = dr * 0.21147017 - dg * 0.52261711 + db * 0.31114694
    delta = 0.5053 * y * y + 0.299 * i * i + 0.1957 * q * q
    return delta > (MAX_DELTA * threshold * threshold)


def yiq_diff_ratio(a: np.ndarray, b: np.ndarray) -> float:
    """Fraction of perceptually different pixels. Named to distinguish it from
    keyframes.diff_ratio, which uses a plain RGB threshold for a different job —
    the two metrics are NOT interchangeable."""
    return float(color_delta_mask(a, b).mean())


def last_frame(video: Path) -> np.ndarray:
    import imageio_ffmpeg

    gen = imageio_ffmpeg.read_frames(str(video), bits_per_pixel=24)
    meta = next(gen)
    w, h = meta["size"]
    tail = None
    n = 0
    for buf in gen:
        tail = buf
        n += 1
    if tail is None:
        raise ValueError("video contains no frames")
    return np.frombuffer(tail, dtype=np.uint8).reshape(h, w, 3)


def crop_to_clip(frame: np.ndarray, clip: dict, ref_shape: tuple[int, int]) -> np.ndarray:
    """Crop a decoded frame to the screenshot's clip region.

    The video is recorded at the viewport's native size, so clip coords map 1:1.
    If a dataset was recorded at a scaled size we rescale rather than fail, so
    the gate still works on older captures.
    """
    fh, fw = frame.shape[:2]
    x, y = int(round(clip["x"])), int(round(clip["y"]))
    w, h = int(round(clip["width"])), int(round(clip["height"]))
    sub = frame[y : y + h, x : x + w]
    if sub.shape[:2] != ref_shape:
        sub = np.asarray(
            Image.fromarray(sub).resize((ref_shape[1], ref_shape[0]), Image.BILINEAR)
        )
    return sub


def check_case(case_dir: Path, case: dict) -> dict:
    video = case_dir / "video.webm"
    result = {"id": case["id"], "group": case["group"]}
    try:
        frame = last_frame(video)
    except Exception as exc:  # noqa: BLE001
        return {**result, "passed": False, "reason": f"cannot decode video: {exc}"}

    if case["group"] == "control":
        # Nothing was injected, so there is no defect to find in the frames.
        return {**result, "passed": True, "reason": "control: no defect to verify"}

    clip = case.get("clip")
    if not clip:
        return {
            **result,
            "passed": False,
            "reason": "gate-report.json has no clip region (dataset predates the video gate)",
        }

    normal = np.asarray(Image.open(case_dir / "normal.png").convert("RGB"))
    buggy = np.asarray(Image.open(case_dir / "buggy.png").convert("RGB"))
    sub = crop_to_clip(frame, clip, normal.shape[:2])

    d_normal = yiq_diff_ratio(sub, normal)
    d_buggy = yiq_diff_ratio(sub, buggy)

    # The final frame always differs a little from buggy.png (the user kept
    # typing after it was taken), so we compare which reference it is closer to
    # rather than demanding an exact match.
    passed = d_buggy * DECISION_MARGIN < d_normal
    return {
        **result,
        "passed": passed,
        "diff_vs_normal": round(d_normal, 4),
        "diff_vs_buggy": round(d_buggy, 4),
        "reason": (
            "last frame resembles the defective state"
            if passed
            else "last frame still looks NORMAL — defect missing from video (truncated tail)"
        ),
    }


def gate_dataset(dataset: Path, cases: list[dict]) -> list[dict]:
    """Run the gate over every case, printing one line each. Returns the results."""
    results = []
    for case in cases:
        r = check_case(dataset / case["path"], case)
        results.append(r)
        mark = "PASS" if r["passed"] else "FAIL"
        extra = ""
        if "diff_vs_normal" in r:
            extra = f"  (vs_normal={r['diff_vs_normal']:.4f} vs_buggy={r['diff_vs_buggy']:.4f})"
        print(f"[{mark}] {r['id']:32s} {r['reason']}{extra}")
    return results


def prune_failed(dataset: Path, meta_path: Path, meta: dict,
                 results: list[dict]) -> None:
    """Delete the rejected case directories and rewrite metadata.json to match,
    so the dataset on disk and its manifest cannot drift apart."""
    keep = {r["id"] for r in results if r["passed"]}
    for case in meta["cases"]:
        if case["id"] not in keep:
            shutil.rmtree(dataset / case["path"], ignore_errors=True)
    meta["cases"] = [c for c in meta["cases"] if c["id"] in keep]
    meta["totalCases"] = len(meta["cases"])
    meta["normalCases"] = sum(1 for c in meta["cases"] if c["group"] == "control")
    meta["buggyCases"] = sum(1 for c in meta["cases"] if c["group"] == "bug")
    paths.write_json(meta_path, meta)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", type=Path)
    ap.add_argument("--prune", action="store_true",
                    help="delete cases that fail and rewrite metadata")
    args = ap.parse_args()

    meta_path = args.dataset / "metadata.json"
    if not meta_path.exists():
        raise SystemExit(f"No metadata.json in {args.dataset}")
    meta = paths.read_json(meta_path)

    results = gate_dataset(args.dataset, meta["cases"])

    failed = [r for r in results if not r["passed"]]
    n_bug = sum(1 for r in results if r["group"] == "bug")
    n_bug_fail = sum(1 for r in failed if r["group"] == "bug")
    print(f"\nVideo gate: {len(results) - len(failed)}/{len(results)} passed "
          f"({n_bug - n_bug_fail}/{n_bug} buggy clips have the defect on video)")

    paths.write_json(args.dataset / "video-gate-report.json", {"results": results})

    if failed and args.prune:
        prune_failed(args.dataset, meta_path, meta, results)
        print(f"Pruned {len(failed)} failed case(s) from {args.dataset}")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
