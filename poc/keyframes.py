"""Frame decoding, sampling strategies, and the pixel structural signal.

Two sampling strategies are implemented, one per experimental condition:

  uniform_indices()   -> Baseline. Evenly spaced frames over the whole clip,
                         the naive "just show the model some frames" approach.
  select_transition() -> Ours. Finds the strongest *sustained* visual
                         transition and returns the before/after pair plus a
                         structural description of what changed.

Threshold note: TodoMVC's white card (#fff) sits on a #f5f5f5 page, only ~10
intensity levels apart, so a coarse threshold is literally blind to the app
vanishing. DIFF_THRESH is set below that gap but above VP8 compression noise.

Nothing in this module reads the ground-truth label — selection is driven only
by the pixels, so it stays honest.
"""

from __future__ import annotations

import numpy as np

# Per-channel intensity delta counted as a changed pixel. See threshold note.
DIFF_THRESH = 8

# Frames before this time are skipped: the page-load paint is a ~97% whole-frame
# change (followed by a decaying tail of late renders) that would swamp every
# other transition. 2.0s was chosen after observing that a 1.2s skip still let
# several clips latch onto the load tail at exactly the boundary frame.
SKIP_LEAD_SECONDS = 2.0


def decode(path: str, stride: int = 3) -> tuple[list[np.ndarray], float]:
    """Decode a webm into a list of RGB frames, keeping every `stride`-th frame.
    Returns (frames, effective_fps)."""
    import imageio_ffmpeg

    gen = imageio_ffmpeg.read_frames(str(path), bits_per_pixel=24)
    meta = next(gen)
    w, h = meta["size"]
    fps = meta["fps"]
    out = []
    for i, buf in enumerate(gen):
        if i % stride:
            continue
        out.append(np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3).astype(np.int16))
    return out, fps / stride


def diff_mask(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.abs(a - b).max(axis=2) > DIFF_THRESH


def diff_ratio(a: np.ndarray, b: np.ndarray) -> float:
    return float(diff_mask(a, b).mean())


def changed_bbox(a: np.ndarray, b: np.ndarray) -> dict | None:
    """Normalised bounding box of the changed region — tells 'one row changed'
    (a legitimate edit) apart from 'the whole card changed' (suspicious)."""
    m = diff_mask(a, b)
    if not m.any():
        return None
    rows = np.where(m.any(axis=1))[0]
    cols = np.where(m.any(axis=0))[0]
    h, w = m.shape
    y0, y1 = int(rows[0]), int(rows[-1])
    x0, x1 = int(cols[0]), int(cols[-1])
    return {
        "x": round(x0 / w, 3),
        "y": round(y0 / h, 3),
        "w": round((x1 - x0 + 1) / w, 3),
        "h": round((y1 - y0 + 1) / h, 3),
        "area_fraction": round(((x1 - x0 + 1) * (y1 - y0 + 1)) / (w * h), 3),
    }


def uniform_indices(n_frames: int, k: int) -> list[int]:
    """Baseline sampling: k evenly spaced frames across the entire clip."""
    if n_frames <= k:
        return list(range(n_frames))
    return [int(round(i * (n_frames - 1) / (k - 1))) for i in range(k)]


def select_transition(frames: list[np.ndarray], fps: float, window: int = 2) -> dict:
    """Ours: locate the strongest sustained transition.

    For each candidate index i we measure how different the state `window`
    frames *before* is from the state `window` frames *after*. A momentary
    blip scores low; a real state change (a todo added, or a defect appearing)
    scores high. We deliberately do NOT bias toward the end of the clip, even
    though injected defects happen to live there — that would exploit a dataset
    artifact rather than detect anything.
    """
    start = max(window, int(SKIP_LEAD_SECONDS * fps))
    end = len(frames) - window
    if end <= start:  # pathologically short clip
        start, end = window, max(window + 1, len(frames) - window)

    scores = []
    for i in range(start, end):
        s = diff_ratio(frames[i - window], frames[i + window])
        scores.append((s, i))
    if not scores:
        i = len(frames) // 2
        scores = [(0.0, i)]

    best_score, best_i = max(scores)
    before = frames[best_i - window]
    after = frames[best_i + window]

    # Oscillation: how many later transitions are also large. A flickering UI
    # keeps changing violently; a settled edit does not.
    big = sum(1 for s, _ in scores if s > max(0.05, best_score * 0.5))

    # Persistence: does the post-transition state still resemble the end of the
    # clip? Both a real edit and a defect persist, so this is descriptive
    # context for the model, not a discriminator on its own.
    persist = 1.0 - diff_ratio(after, frames[-1])

    return {
        "index": best_i,
        "time_s": round(best_i / fps, 2),
        "before": before,
        "after": after,
        # The settled end state. A genuine defect persists to the end of the
        # recording, so this frame guarantees the defect is in the payload even
        # when the transition detector locks onto a user action instead of the
        # injection (observed on one blur clip). Not a leak: normal clips also
        # have a final state, and the baseline's uniform sample includes the
        # last frame too.
        "final": frames[-1],
        "signal": {
            "change_ratio": round(best_score, 4),
            "changed_region": changed_bbox(before, after),
            "large_transition_count": int(big),
            "post_state_persistence": round(float(persist), 3),
            "clip_duration_s": round(len(frames) / fps, 2),
        },
    }
