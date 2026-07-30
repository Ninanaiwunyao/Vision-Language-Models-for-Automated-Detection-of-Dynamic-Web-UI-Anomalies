"""EXP002 condition codes, and the trial config that lists them.

A condition is a 2-factor code `AxBy`:

  Factor A — which frames are sent to the model:
    A0  uniform sampling (no selection)
    A1  pixel-diff-only keyframe selection
    A3  pixel + DOM fused selection
  Factor B — the prompt:
    B0  naive prompt
    B1  dual-path decision prompt

run_trial.py executes conditions and score_trial.py scores them, so both parse
these codes. The grammar, the "is this level implemented" check and the config
loader live here — one place to edit when a factor level is added.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from paths import read_json

CODE_RE = re.compile(r"^(A\d)(B\d)$")

FRAME_SELECTORS = ("A0", "A1", "A3")
PROMPT_VARIANTS = ("B0", "B1")


@dataclass(frozen=True)
class TrialConfig:
    """The parts of a frozen experiment spec the trial runner actually consumes."""

    conditions: list[str]
    repeats: int
    generation_config: dict | None
    source: str

    def with_conditions(self, codes: list[str]) -> "TrialConfig":
        validate(codes)
        return replace(self, conditions=codes)


def split(code: str) -> tuple[str, str]:
    """'A3B1' -> ('A3', 'B1'). Assumes the code already passed validate()."""
    return CODE_RE.match(code).groups()  # type: ignore[union-attr]


def validate(codes: list[str]) -> None:
    """Reject any code the pipeline cannot actually run, with a clear reason."""
    if not codes:
        raise SystemExit("No conditions to run")
    for code in codes:
        m = CODE_RE.match(code)
        if not m:
            raise SystemExit(f"Malformed condition code '{code}' (expected AxBy)")
        a, b = m.group(1), m.group(2)
        if a not in FRAME_SELECTORS:
            raise SystemExit(f"{code}: frame selector {a} not implemented")
        if b not in PROMPT_VARIANTS:
            raise SystemExit(f"{code}: prompt variant {b} not implemented")
        if b == "B1" and a == "A0":
            raise SystemExit(
                f"{code}: B1 (dual-path) needs a transition-based selector; "
                f"A0 (uniform) has no transition to describe"
            )


def load(path: Path) -> TrialConfig:
    """Read a frozen experiment spec into a TrialConfig."""
    if not path.exists():
        raise SystemExit(f"Conditions config not found: {path}")
    cfg = read_json(path)
    codes = cfg.get("conditions")
    if not codes:
        raise SystemExit(f"No 'conditions' list in {path}")
    validate(codes)
    # Strip comment keys from the generation config before it reaches the SDK.
    gen = {k: v for k, v in cfg.get("generation_config", {}).items()
           if not k.startswith("_")}
    return TrialConfig(
        conditions=codes,
        repeats=int(cfg.get("repeats", 1)),
        generation_config=gen or None,
        source=str(path),
    )
