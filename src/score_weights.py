"""External score-weight configuration for the composite hotspot detector.

The composite detector combines multiple sub-detectors (audio_rms, scene
changes, comment density, transcripts, etc.) into a single weighted score.
The relative weights are deliberately externalised so non-engineers can
tune them without touching code:

  * ``--weights <path>``           – load a JSON config
  * ``--interactive-weights``      – prompt on stdin, optionally save to file

JSON schema (extensible — unknown keys are ignored):

::

    {
      "detectors": [
        {"name": "audio_rms",       "weight": 1.0},
        {"name": "comment_density", "weight": 2.0}
      ],
      "bin_seconds": 1.0,
      "min_score":   0.0
    }
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class DetectorWeight:
    name: str
    weight: float


@dataclass
class Weights:
    detectors: list[DetectorWeight] = field(default_factory=list)
    bin_seconds: float = 1.0
    min_score: float = 0.0

    def enabled(self) -> list[DetectorWeight]:
        """Detectors with weight > 0, in declared order."""
        return [d for d in self.detectors if d.weight > 0]

    def names(self) -> list[str]:
        return [d.name for d in self.detectors]

    def to_dict(self) -> dict:
        return {
            "detectors": [asdict(d) for d in self.detectors],
            "bin_seconds": self.bin_seconds,
            "min_score": self.min_score,
        }


class WeightsConfigError(ValueError):
    """Raised when a weights config file is malformed."""


def default_weights() -> Weights:
    """Reasonable defaults for the detectors that exist today."""
    return Weights(
        detectors=[
            DetectorWeight(name="audio_rms", weight=1.0),
            DetectorWeight(name="comment_density", weight=1.0),
        ],
        bin_seconds=1.0,
        min_score=0.0,
    )


def load_weights(path: Path) -> Weights:
    if not path.exists():
        raise WeightsConfigError(f"weights file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise WeightsConfigError(f"invalid JSON in {path}: {e}") from e

    if not isinstance(raw, dict):
        raise WeightsConfigError(f"{path}: top-level must be a JSON object")

    detectors_raw = raw.get("detectors", [])
    if not isinstance(detectors_raw, list):
        raise WeightsConfigError(f"{path}: 'detectors' must be a list")

    detectors: list[DetectorWeight] = []
    for i, entry in enumerate(detectors_raw):
        if not isinstance(entry, dict) or "name" not in entry:
            raise WeightsConfigError(
                f"{path}: detectors[{i}] must be an object with at least 'name'"
            )
        try:
            weight = float(entry.get("weight", 1.0))
        except (TypeError, ValueError) as e:
            raise WeightsConfigError(
                f"{path}: detectors[{i}].weight must be a number"
            ) from e
        detectors.append(DetectorWeight(name=str(entry["name"]), weight=weight))

    try:
        bin_seconds = float(raw.get("bin_seconds", 1.0))
        min_score = float(raw.get("min_score", 0.0))
    except (TypeError, ValueError) as e:
        raise WeightsConfigError(f"{path}: bin_seconds / min_score must be numbers") from e

    return Weights(detectors=detectors, bin_seconds=bin_seconds, min_score=min_score)


def save_weights(path: Path, weights: Weights) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(weights.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def interactive_weights(
    available: list[str],
    *,
    defaults: Weights | None = None,
    in_stream=None,
    out_stream=None,
) -> Weights:
    """Prompt on stdin for each available detector's weight.

    ``available`` is the list of detector names currently registered in the
    factory. ``defaults`` (if given) seeds the prompt values; otherwise
    every detector defaults to weight 1.0.
    """
    in_stream = in_stream or sys.stdin
    out_stream = out_stream or sys.stdout

    seed: dict[str, float] = {}
    if defaults is not None:
        seed = {d.name: d.weight for d in defaults.detectors}
    seed_bin = defaults.bin_seconds if defaults else 1.0
    seed_min = defaults.min_score if defaults else 0.0

    print("[Composite detector weights]", file=out_stream)
    print(
        "Enter a relative weight for each detector. "
        "Leave blank to keep the default. 0 disables that detector.",
        file=out_stream,
    )

    chosen: list[DetectorWeight] = []
    for name in available:
        default = seed.get(name, 1.0)
        prompt = f"  {name:<18} (default {default:g}): "
        out_stream.write(prompt)
        out_stream.flush()
        line = in_stream.readline().strip()
        if not line:
            chosen.append(DetectorWeight(name=name, weight=default))
            continue
        try:
            chosen.append(DetectorWeight(name=name, weight=float(line)))
        except ValueError:
            print(
                f"  ! could not parse '{line}' as a number — using default {default:g}",
                file=out_stream,
            )
            chosen.append(DetectorWeight(name=name, weight=default))

    def _read_float(label: str, default: float) -> float:
        out_stream.write(f"  {label:<18} (default {default:g}): ")
        out_stream.flush()
        line = in_stream.readline().strip()
        if not line:
            return default
        try:
            return float(line)
        except ValueError:
            print(
                f"  ! could not parse '{line}' as a number — using default {default:g}",
                file=out_stream,
            )
            return default

    bin_seconds = _read_float("bin_seconds", seed_bin)
    min_score = _read_float("min_score", seed_min)

    return Weights(detectors=chosen, bin_seconds=bin_seconds, min_score=min_score)


def maybe_save_interactive(weights: Weights, *, in_stream=None, out_stream=None) -> Path | None:
    """Ask whether to persist the just-entered weights. Returns saved path or None."""
    in_stream = in_stream or sys.stdin
    out_stream = out_stream or sys.stdout

    out_stream.write("Save these weights for next time? [y/N]: ")
    out_stream.flush()
    answer = in_stream.readline().strip().lower()
    if answer not in {"y", "yes"}:
        return None

    out_stream.write("  Path [weights.json]: ")
    out_stream.flush()
    path_str = in_stream.readline().strip() or "weights.json"
    path = Path(path_str)
    save_weights(path, weights)
    print(f"Saved to {path}", file=out_stream)
    return path
