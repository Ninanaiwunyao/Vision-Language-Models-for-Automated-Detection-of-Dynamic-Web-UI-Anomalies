"""Shared Gemini client for the PoC trial.

Experimental-validity rule enforced here: the model is resolved **once** and
then frozen for the whole trial. On a 429 we retry the *same* model with
backoff rather than falling back to another one — silently switching models
between clips would mean different cases were judged by different judges, which
destroys the paired comparison.
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
import warnings

import numpy as np
from PIL import Image

warnings.filterwarnings("ignore", category=FutureWarning)

API_KEY_ENV = ("GOOGLE_API_KEY", "GEMINI_API_KEY")
MODEL_ENV = "GEMINI_MODEL"


class QuotaExhausted(RuntimeError):
    pass


def get_api_key() -> str:
    for name in API_KEY_ENV:
        val = os.environ.get(name)
        if val:
            return val
    raise SystemExit(
        f"No API key found. Export one of: {', '.join(API_KEY_ENV)}"
    )


def is_quota_error(exc: Exception) -> bool:
    s = str(exc)
    return "429" in s or "RESOURCE_EXHAUSTED" in s or "quota" in s.lower()


def is_unavailable_error(exc: Exception) -> bool:
    """The list-models endpoint advertises models that generate_content then
    rejects (404 'no longer available to new users'). Such a candidate is dead
    for this key, exactly like a zero-quota one."""
    s = str(exc)
    return "404" in s or "NOT_FOUND" in s or "no longer available" in s.lower()


def _cost_rank(name: str) -> tuple[int, str]:
    """Cheapest first: *-flash-lite < *-flash < everything else.
    Within a tier, sort by name for determinism."""
    if "flash-lite" in name:
        tier = 0
    elif "flash" in name:
        tier = 1
    else:
        tier = 2
    return (tier, name)


def _probe(genai, name: str) -> str | None:
    """One tiny call to prove this model is actually usable with this key.
    Returns None if it works, or a short skip-reason if the candidate is dead.

    'Cheapest available' is meaningless on the free tier: a listed model can
    carry a hard `limit: 0` quota (this key's entire gemini-2.0 family), or be
    rejected outright with 404 'no longer available to new users'
    (gemini-2.5-flash-lite). Both must be eliminated BEFORE the trial freezes
    its model, not discovered after.
    """
    try:
        genai.GenerativeModel(name).generate_content(
            "ping", generation_config={"max_output_tokens": 1}
        )
        return None
    except Exception as exc:  # noqa: BLE001
        if is_quota_error(exc):
            return "no quota (429, limit exhausted)"
        if is_unavailable_error(exc):
            return "listed but not servable (404 for this account)"
        raise  # anything else is a real bug; never mask it


def connect(verbose: bool = True):
    """Configure the SDK and freeze the cheapest model THAT HAS QUOTA (probed
    once, in cost order). Returns (genai_module, model_name)."""
    import google.generativeai as genai

    genai.configure(api_key=get_api_key())
    names = [
        m.name.split("/")[-1]
        for m in genai.list_models()
        if "generateContent" in getattr(m, "supported_generation_methods", [])
    ]
    override = os.environ.get(MODEL_ENV)
    if override:
        if override not in names:
            raise SystemExit(
                f"Model '{override}' not available. Available: {', '.join(sorted(names))}"
            )
        candidates = [override]  # explicit pin skips probing order but is still verified
    else:
        usable = [n for n in names if "flash" in n]
        if not usable:
            raise SystemExit(
                f"No flash model available. Available: {', '.join(sorted(names))}"
            )
        candidates = sorted(usable, key=_cost_rank)

    chosen = None
    for name in candidates:
        if verbose:
            print(f"  probing {name} ...", end=" ", flush=True)
        skip_reason = _probe(genai, name)
        if skip_reason is None:
            if verbose:
                print("ok")
            chosen = name
            break
        if verbose:
            print(f"{skip_reason} — skipping")
    if chosen is None:
        raise SystemExit(
            "Every candidate model is quota-exhausted for this key. "
            "Enable billing, wait for the daily reset, or pin GEMINI_MODEL "
            "to a model you know has quota."
        )
    if verbose:
        print(f"Model (frozen for the whole trial): {chosen}")
    return genai, chosen


def frame_to_part(arr: np.ndarray, quality: int = 90) -> dict:
    """Encode an RGB ndarray as a JPEG inline image part. JPEG keeps the upload
    small; Gemini bills images by dimension, so quality costs nothing extra."""
    img = Image.fromarray(arr.astype(np.uint8), mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return {
        "mime_type": "image/jpeg",
        "data": base64.b64encode(buf.getvalue()).decode("ascii"),
    }


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t[3:]
        if t.endswith("```"):
            t = t[: t.rfind("```")]
    return t.strip()


def _usage_of(resp) -> dict | None:
    """Extract per-call token counts from the response, if the SDK reports them.
    Needed for EXP002's cost/efficiency metrics; absent on some error paths."""
    u = getattr(resp, "usage_metadata", None)
    if u is None:
        return None
    return {
        "prompt_tokens": getattr(u, "prompt_token_count", None),
        "candidates_tokens": getattr(u, "candidates_token_count", None),
        "total_tokens": getattr(u, "total_token_count", None),
    }


def call_json(
    genai,
    model_name: str,
    parts: list,
    *,
    generation_config: dict | None = None,
    max_retries: int = 5,
    base_sleep: float = 20.0,
) -> dict:
    """Send `parts`, expect strict JSON back, retry the SAME model on 429.

    `generation_config` is passed straight to the SDK (EXP002 pins temperature
    here so decoding is a controlled variable, not the SDK default of 1.0).

    Returns the parsed dict, plus `_raw` (raw text, so a malformed reply is
    still auditable) and `_usage` (per-call token counts, or None).
    """
    model = genai.GenerativeModel(model_name)
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = model.generate_content(parts, generation_config=generation_config)
            usage = _usage_of(resp)
            raw = _strip_code_fence(resp.text or "")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                # Record the failure instead of crashing the whole trial: an
                # unparseable answer is itself a result worth counting.
                return {"verdict": None, "reason": "unparseable", "_raw": raw,
                        "_usage": usage}
            parsed["_raw"] = raw
            parsed["_usage"] = usage
            return parsed
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if is_quota_error(exc):
                wait = base_sleep * (attempt + 1)
                print(f"      429 quota — retry {attempt + 1}/{max_retries} in {wait:.0f}s ...")
                time.sleep(wait)
                continue
            raise
    raise QuotaExhausted(f"still rate-limited after {max_retries} retries: {last_exc}")
