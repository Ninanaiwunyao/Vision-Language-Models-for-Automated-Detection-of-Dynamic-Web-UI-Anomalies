#!/usr/bin/env python3
"""Stage 2 — Four-stage VLM smoke test.

Before spending time on the actual PoC evaluation we must prove the plumbing
works end to end. If the smoke test fails we know it is the *pipeline* that is
broken, not the method — otherwise a bad accuracy number is unattributable.

The four stages, run in order and short-circuiting on the first failure:

  1. NETWORK   — can we reach the Gemini API host at all (DNS + TLS + HTTP)?
  2. AUTH      — is the API key present and accepted (list the models)?
  3. IMAGE     — can we load a dataset frame, base64-encode it, and have the
                 multimodal model actually receive and describe it?
  4. JSON      — can we make the model return strict JSON and parse it?

Model selection: unless GEMINI_MODEL pins one, stages 3-4 try a prioritised
list of *-flash models and fall back to the next one on a 429 quota error, so a
single model with a zeroed-out free tier does not sink the whole run.

Usage:
  export GOOGLE_API_KEY=...            # or GEMINI_API_KEY
  python3 poc/smoke_test.py           # auto-picks a model with quota
  GEMINI_MODEL=gemini-2.5-flash python3 poc/smoke_test.py   # pin one model

Exit code 0 means the gate is GREEN and it is safe to start the PoC.
Any non-zero exit means DO NOT proceed — read the printed stage to see why.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

POC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(POC_DIR))

import paths  # noqa: E402
# The auth, quota-detection and code-fence helpers are the ones the real trial
# uses, imported rather than re-implemented so a green smoke test is evidence
# about the actual pipeline. vlm_client keeps its heavy imports lazy, so this
# costs nothing here and still lets stage 2 diagnose a missing SDK itself.
from vlm_client import (  # noqa: E402
    API_KEY_ENV, MODEL_ENV, find_api_key, is_quota_error, strip_code_fence,
)
from paths import MANIFEST_JSON, REPO_ROOT  # noqa: E402

API_HOST = "generativelanguage.googleapis.com"

# Preference order for auto-selection: models most likely to still carry free
# tier quota first. Any of these that the account exposes are tried in turn on a
# 429 before we give up. Models not in this list (but still *-flash) are tried
# afterwards; non-flash models are ignored for the image task.
PREFERRED_MODELS = (
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemini-1.5-flash",
    "gemini-2.0-flash",
)

# Cheap, unambiguous console markers (avoid unicode issues in some terminals).
OK = "[ PASS ]"
NO = "[ FAIL ]"


class AllQuotaExhausted(Exception):
    """Raised when every candidate model returns a 429 quota error."""

    def __init__(self, original: Exception | None):
        super().__init__("all candidate models are quota-exhausted")
        self.original = original


def fail(stage: str, msg: str, hint: str | None = None) -> "None":
    print(f"{NO} Stage {stage}: {msg}")
    if hint:
        print(f"        -> {hint}")
    print("\nSMOKE TEST FAILED — do not start the PoC until this is green.")
    sys.exit(1)


def pick_sample_image() -> Path:
    """Use the first anomalous keyframe from the manifest so the smoke test
    exercises the same asset type the PoC will feed the model."""
    if MANIFEST_JSON.exists():
        doc = paths.read_json(MANIFEST_JSON)
        for case in doc.get("cases", []):
            if case.get("is_anomaly"):
                p = REPO_ROOT / case["keyframes"]["buggy"]
                if p.exists():
                    return p
    for p in (REPO_ROOT / "dataset_v1" / "buggy").glob("*/buggy.png"):
        return p
    raise SystemExit("No sample image found; run build_manifest.py first.")


def order_candidates(available: list[str], override: str | None) -> list[str]:
    """Build the list of models to try, most-likely-to-have-quota first.
    An explicit GEMINI_MODEL override disables fallback (single-element list)."""
    if override:
        return [override]
    ordered = [m for m in PREFERRED_MODELS if m in available]
    # Append any other flash models the account has that we didn't list.
    ordered += sorted(
        m for m in available if "flash" in m and m not in ordered
    )
    return ordered


def generate_with_fallback(genai, candidates: list[str], parts: list):
    """Try each candidate model; skip to the next only on a 429 quota error.
    Any other error is a genuine pipeline failure and is re-raised immediately.
    Returns (model_name, model, response)."""
    last_quota_err: Exception | None = None
    for name in candidates:
        model = genai.GenerativeModel(name)
        try:
            resp = model.generate_content(parts)
            return name, model, resp
        except Exception as exc:  # noqa: BLE001
            if is_quota_error(exc):
                print(f"        quota exhausted on '{name}' (429) — trying next model ...")
                last_quota_err = exc
                continue
            raise
    raise AllQuotaExhausted(last_quota_err)


# ---------------------------------------------------------------------------
# Stage 1 — network reachability
# ---------------------------------------------------------------------------
def stage_network() -> None:
    print("Stage 1/4  NETWORK  — reaching the Gemini API host ...")
    try:
        import requests
    except ImportError:
        fail("1 (NETWORK)", "the 'requests' package is not installed",
             "pip install requests")
    try:
        # No key needed: an unauthenticated hit still proves DNS+TLS+HTTP work.
        resp = requests.get(f"https://{API_HOST}/", timeout=15)
        print(f"{OK} Stage 1: reachable (HTTP {resp.status_code} from {API_HOST})")
    except requests.exceptions.RequestException as exc:
        fail("1 (NETWORK)", f"cannot reach {API_HOST}: {exc}",
             "check your internet connection / proxy / firewall")


# ---------------------------------------------------------------------------
# Stage 2 — API key / auth. Returns (genai module, ordered candidate models).
# ---------------------------------------------------------------------------
def stage_auth() -> tuple[object, list[str]]:
    print("Stage 2/4  AUTH     — validating API key and listing models ...")
    key = find_api_key()
    if not key:
        fail("2 (AUTH)", "no API key found in the environment",
             f"export one of: {', '.join(API_KEY_ENV)}")
    try:
        import google.generativeai as genai
    except ImportError:
        fail("2 (AUTH)", "the 'google-generativeai' package is not installed",
             "pip install google-generativeai")

    genai.configure(api_key=key)
    try:
        models = [m for m in genai.list_models()
                  if "generateContent" in getattr(m, "supported_generation_methods", [])]
    except Exception as exc:  # noqa: BLE001
        fail("2 (AUTH)", f"the API rejected the key or the call failed: {exc}",
             "verify the key is valid and has the Generative Language API enabled")

    if not models:
        fail("2 (AUTH)", "key works but no models support generateContent")

    names = [m.name.split("/")[-1] for m in models]
    override = os.environ.get(MODEL_ENV)
    if override and override not in names:
        fail("2 (AUTH)", f"requested model '{override}' not available to this key",
             f"available: {', '.join(sorted(names))}")

    candidates = order_candidates(names, override)
    if not candidates:
        fail("2 (AUTH)", "no *-flash model is available to this key",
             f"available: {', '.join(sorted(names))}; set GEMINI_MODEL explicitly")
    print(f"{OK} Stage 2: key accepted, {len(names)} models available; "
          f"will try: {', '.join(candidates)}")
    return genai, candidates


# ---------------------------------------------------------------------------
# Stage 3 — image base64 encoding + multimodal round-trip
# ---------------------------------------------------------------------------
def stage_image(genai, candidates: list[str], img_path: Path) -> tuple[object, str]:
    print("Stage 3/4  IMAGE    — base64-encoding a frame and sending it ...")
    try:
        raw = img_path.read_bytes()
        b64 = base64.b64encode(raw).decode("ascii")
        assert base64.b64decode(b64) == raw, "base64 round-trip mismatch"
    except Exception as exc:  # noqa: BLE001
        fail("3 (IMAGE)", f"failed to read/encode {img_path}: {exc}")
    print(f"        encoded {len(raw):,} bytes -> {len(b64):,} b64 chars "
          f"({img_path.relative_to(REPO_ROOT)})")

    image_part = {"mime_type": "image/png", "data": b64}
    prompt = "In one short sentence, describe what this UI screenshot shows."
    try:
        model_name, model, resp = generate_with_fallback(
            genai, candidates, [prompt, image_part]
        )
        text = (resp.text or "").strip()
    except AllQuotaExhausted as exc:
        fail("3 (IMAGE)", "every candidate model returned 429 (quota exhausted)",
             "This API key has no free-tier quota left for these models. Enable "
             "billing on the Google Cloud project, wait for the daily quota to "
             "reset, or set GEMINI_MODEL to one you have quota for. "
             f"Last error: {exc.original}")
    except Exception as exc:  # noqa: BLE001
        fail("3 (IMAGE)", f"the multimodal request failed: {exc}",
             "the model may not accept inline images, or the payload was rejected")
    if not text:
        fail("3 (IMAGE)", "the model returned an empty response for the image")
    print(f"{OK} Stage 3: '{model_name}' saw the image -> \"{text[:110]}\"")
    return model, model_name


# ---------------------------------------------------------------------------
# Stage 4 — strict JSON response + parse (reuses the model that worked above)
# ---------------------------------------------------------------------------
def stage_json(model, model_name: str, img_path: Path) -> None:
    print(f"Stage 4/4  JSON     — requesting strict JSON from '{model_name}' ...")
    prompt = (
        "Look at this UI screenshot and decide if it shows a visual defect. "
        "Respond with ONLY a JSON object, no prose, no code fence, with exactly "
        'these keys: {"verdict": "normal" | "anomaly", "reason": string}.'
    )
    b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
    raw = ""
    try:
        resp = model.generate_content(
            [prompt, {"mime_type": "image/png", "data": b64}]
        )
        raw = strip_code_fence(resp.text or "")
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail("4 (JSON)", f"response was not valid JSON: {exc}",
             f"raw model output was: {raw!r}")
    except Exception as exc:  # noqa: BLE001
        if is_quota_error(exc):
            fail("4 (JSON)", f"'{model_name}' hit its quota (429) on the JSON call",
                 "daily quota may be exhausted; retry later or enable billing")
        fail("4 (JSON)", f"the request failed: {exc}")

    if "verdict" not in parsed or "reason" not in parsed:
        fail("4 (JSON)", f"JSON parsed but missing required keys: {parsed}")
    print(f"{OK} Stage 4: parsed JSON -> verdict={parsed['verdict']!r}")


def main() -> None:
    print("=" * 64)
    print("PoC VLM smoke test — proving the pipeline before the experiment")
    print("=" * 64)

    stage_network()
    genai, candidates = stage_auth()
    sample = pick_sample_image()
    model, model_name = stage_image(genai, candidates, sample)
    stage_json(model, model_name, sample)

    print("\n" + "=" * 64)
    print("SMOKE TEST GREEN — network, key, image encoding and JSON all pass.")
    print(f"Working model: {model_name}. It is safe to start the PoC evaluation.")
    print("=" * 64)


if __name__ == "__main__":
    main()
