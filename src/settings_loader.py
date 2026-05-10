"""Load a unified settings JSON file for non-engineer ergonomics.

Without this loader, a typical run looks like::

    python -m src.main --input samples/v.mp4 --output output/ \\
        --detector composite --weights weights.json \\
        --chat-log chat.json --candidates 6 --window 30 --debug

Which is a lot to remember. With it, the same run becomes::

    python -m src.main --settings settings.json

…where ``settings.json`` carries every commonly-tuned field plus an inline
``weights`` block. CLI flags still win when both are specified, so engineers
can override any single field for a one-off experiment.

Schema (only ``input`` and ``output`` are conceptually required for a full
run; everything else has a sensible CLI default):

  - ``input``         (str)    — path to input video
  - ``output``        (str)    — output directory
  - ``detector``      (str)    — even / audio_rms / comment_density / composite
  - ``candidates``    (int)
  - ``window``        (float)
  - ``min_duration``  (float)
  - ``max_duration``  (float)
  - ``chat_log``      (str)    — path to chat-log JSON
  - ``export_clips``  (bool)
  - ``debug``         (bool)
  - ``weights``       (object) — inline; same schema as weights.example.json
  - ``weights_path``  (str)    — alternative to ``weights``: load from a path

Keys starting with ``_comment`` (and any other unknown keys) are ignored so
the file can carry inline notes for non-engineers.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SettingsLoadError(ValueError):
    """Raised when a settings file is malformed or contradicts itself."""


# Maps the keys we recognise in settings.json onto the argparse ``dest`` names
# used by the CLI parser. ``argparse`` derives dest from ``--flag-name`` by
# replacing dashes with underscores; we mirror that here so callers can pass
# the result straight to ``parser.set_defaults(**...)``.
_KEY_TO_DEST: dict[str, str] = {
    "input": "input",
    "output": "output",
    "detector": "detector",
    "candidates": "candidates",
    "window": "window",
    "min_duration": "min_duration",
    "max_duration": "max_duration",
    "chat_log": "chat_log",
    "export_clips": "export_clips",
    "debug": "debug",
    "weights_path": "weights",  # CLI flag is --weights, dest is "weights"
}

_PATH_DESTS = {"input", "output", "chat_log", "weights"}


def load_settings(path: Path) -> tuple[dict[str, Any], dict | None]:
    """Read ``path`` and return ``(parser_defaults, inline_weights)``.

    ``parser_defaults`` is suitable for ``argparse.ArgumentParser.set_defaults``.
    ``inline_weights`` is the raw ``weights`` dict (or ``None``) for the
    score_weights loader to consume directly without touching the filesystem.
    """
    if not path.exists():
        raise SettingsLoadError(f"settings file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SettingsLoadError(f"invalid JSON in {path}: {e}") from e
    if not isinstance(raw, dict):
        raise SettingsLoadError(f"{path}: top-level must be a JSON object")

    if "weights" in raw and "weights_path" in raw:
        raise SettingsLoadError(
            f"{path}: only one of 'weights' (inline) or 'weights_path' may be set"
        )

    defaults: dict[str, Any] = {}
    for key, dest in _KEY_TO_DEST.items():
        if key not in raw:
            continue
        value = raw[key]
        if dest in _PATH_DESTS and isinstance(value, str):
            value = Path(value)
        defaults[dest] = value

    inline_weights = raw.get("weights")
    if inline_weights is not None and not isinstance(inline_weights, dict):
        raise SettingsLoadError(
            f"{path}: 'weights' must be a JSON object (or omitted)"
        )

    return defaults, inline_weights
