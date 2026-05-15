"""Load ``config.yaml`` for the inbox workflow.

Layered config resolution:

  1. Hard-coded defaults in this module (every field has one)
  2. ``config.yaml`` on disk (overrides any subset)
  3. Per-task ``*.task.yaml`` (resolved in ``src/inbox.py``)

Schema (all top-level sections optional — anything missing falls back to
its default, so a minimal ``config.yaml`` can be just::

    defaults:
      detector: audio_rms

The full shape::

    paths:
      inbox:   ./inbox
      output:  ./output
      archive: ./archive
      failed:  ./failed

    naming:
      dir:
        include:        # components to include in output dir name
          date: true
          streamer: true
          purpose: true
          title: false
          detector: false
          task: true    # task-file stem; on guarantees uniqueness
        order: [date, streamer, purpose, title, detector, task]
        separator: "_"
        date_format: "%Y-%m-%d"
        slug_max_length: 40
        on_conflict: suffix      # "suffix" → _2,_3; "error" → raise

      clip:
        include:
          index: true
          slug: true
          detector: false
          timestamp: false
        order: [index, slug, detector, timestamp]
        separator: "_"
        index_format: "{:02d}"

    defaults:
      detector: composite
      candidates: 6
      window: 30
      min_duration: 10
      max_duration: 60
      export_clips: true
      export_thumbnails: true
      debug: false
      weights: { ... }        # passed verbatim to parse_weights_dict
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigLoadError(ValueError):
    """Raised when ``config.yaml`` is malformed or contradicts itself."""


# Known toggleable components — anything outside this set in a config file
# is an error (catches typos like "streemer" early instead of silently
# producing a directory missing the streamer component).
KNOWN_DIR_COMPONENTS: tuple[str, ...] = (
    "date", "streamer", "purpose", "title", "detector", "task",
)
KNOWN_CLIP_COMPONENTS: tuple[str, ...] = (
    "index", "slug", "detector", "timestamp",
)

DEFAULT_DIR_INCLUDE: dict[str, bool] = {
    "date": True,
    "streamer": True,
    "purpose": True,
    "title": False,
    "detector": False,
    "task": True,
}
DEFAULT_DIR_ORDER: tuple[str, ...] = (
    "date", "streamer", "purpose", "title", "detector", "task",
)
DEFAULT_CLIP_INCLUDE: dict[str, bool] = {
    "index": True,
    "slug": True,
    "detector": False,
    "timestamp": False,
}
DEFAULT_CLIP_ORDER: tuple[str, ...] = (
    "index", "slug", "detector", "timestamp",
)

ON_CONFLICT_MODES: tuple[str, ...] = ("suffix", "error")


@dataclass
class PathsConfig:
    inbox: Path = Path("./inbox")
    output: Path = Path("./output")
    archive: Path = Path("./archive")
    failed: Path = Path("./failed")


@dataclass
class NamingDirConfig:
    include: dict[str, bool] = field(
        default_factory=lambda: dict(DEFAULT_DIR_INCLUDE)
    )
    order: list[str] = field(default_factory=lambda: list(DEFAULT_DIR_ORDER))
    separator: str = "_"
    date_format: str = "%Y-%m-%d"
    slug_max_length: int = 40
    on_conflict: str = "suffix"


@dataclass
class NamingClipConfig:
    include: dict[str, bool] = field(
        default_factory=lambda: dict(DEFAULT_CLIP_INCLUDE)
    )
    order: list[str] = field(default_factory=lambda: list(DEFAULT_CLIP_ORDER))
    separator: str = "_"
    index_format: str = "{:02d}"


@dataclass
class NamingConfig:
    dir: NamingDirConfig = field(default_factory=NamingDirConfig)
    clip: NamingClipConfig = field(default_factory=NamingClipConfig)


@dataclass
class DefaultsConfig:
    detector: str = "even"
    candidates: int = 6
    window: float = 30.0
    min_duration: float = 10.0
    max_duration: float = 60.0
    export_clips: bool = False
    export_thumbnails: bool = False
    debug: bool = False
    # Raw weights block — fed verbatim to ``score_weights.parse_weights_dict``
    # so this module doesn't have to know the weights schema. ``None`` means
    # "no weights configured"; composite detector will refuse to run.
    weights: dict[str, Any] | None = None


@dataclass
class Config:
    paths: PathsConfig = field(default_factory=PathsConfig)
    naming: NamingConfig = field(default_factory=NamingConfig)
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    source_path: Path | None = None


# ---------------------------------------------------------------------------
# Type checks. Booleans are an int subclass in Python, but for fields where
# we want a strict int (e.g. slug_max_length) we reject bool explicitly so
# `slug_max_length: true` doesn't silently become 1.
# ---------------------------------------------------------------------------

def _require_str(value: Any, field: str, source: str) -> str:
    if not isinstance(value, str):
        raise ConfigLoadError(
            f"{source}: '{field}' must be a string, got {type(value).__name__}"
        )
    return value


def _require_bool(value: Any, field: str, source: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigLoadError(
            f"{source}: '{field}' must be true/false, got {type(value).__name__}"
        )
    return value


def _require_int(value: Any, field: str, source: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigLoadError(
            f"{source}: '{field}' must be an integer, got {type(value).__name__}"
        )
    return value


def _require_number(value: Any, field: str, source: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigLoadError(
            f"{source}: '{field}' must be a number, got {type(value).__name__}"
        )
    return float(value)


def _require_dict(value: Any, field: str, source: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigLoadError(
            f"{source}: '{field}' must be a mapping, got {type(value).__name__}"
        )
    return value


def _require_list(value: Any, field: str, source: str) -> list:
    if not isinstance(value, list):
        raise ConfigLoadError(
            f"{source}: '{field}' must be a list, got {type(value).__name__}"
        )
    return value


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------

def _parse_paths(raw: dict, source: str) -> PathsConfig:
    cfg = PathsConfig()
    for key in ("inbox", "output", "archive", "failed"):
        if key in raw:
            s = _require_str(raw[key], f"paths.{key}", source)
            setattr(cfg, key, Path(s))
    return cfg


def _parse_naming_dir(raw: dict, source: str) -> NamingDirConfig:
    cfg = NamingDirConfig()
    if "include" in raw:
        include = _require_dict(raw["include"], "naming.dir.include", source)
        for key, val in include.items():
            if key not in KNOWN_DIR_COMPONENTS:
                raise ConfigLoadError(
                    f"{source}: unknown naming.dir.include key {key!r}. "
                    f"Known components: {', '.join(KNOWN_DIR_COMPONENTS)}"
                )
            cfg.include[key] = _require_bool(val, f"naming.dir.include.{key}", source)
    if "order" in raw:
        order = _require_list(raw["order"], "naming.dir.order", source)
        for item in order:
            if not isinstance(item, str) or item not in KNOWN_DIR_COMPONENTS:
                raise ConfigLoadError(
                    f"{source}: unknown naming.dir.order item {item!r}. "
                    f"Known components: {', '.join(KNOWN_DIR_COMPONENTS)}"
                )
        cfg.order = list(order)
    if "separator" in raw:
        cfg.separator = _require_str(raw["separator"], "naming.dir.separator", source)
    if "date_format" in raw:
        cfg.date_format = _require_str(raw["date_format"], "naming.dir.date_format", source)
    if "slug_max_length" in raw:
        n = _require_int(raw["slug_max_length"], "naming.dir.slug_max_length", source)
        if n < 1:
            raise ConfigLoadError(
                f"{source}: naming.dir.slug_max_length must be >= 1, got {n}"
            )
        cfg.slug_max_length = n
    if "on_conflict" in raw:
        mode = _require_str(raw["on_conflict"], "naming.dir.on_conflict", source)
        if mode not in ON_CONFLICT_MODES:
            raise ConfigLoadError(
                f"{source}: naming.dir.on_conflict must be one of "
                f"{', '.join(ON_CONFLICT_MODES)}, got {mode!r}"
            )
        cfg.on_conflict = mode
    if not any(cfg.include.get(c, False) for c in cfg.order):
        raise ConfigLoadError(
            f"{source}: naming.dir requires at least one include component to be true "
            "(otherwise the output directory name would be empty)"
        )
    return cfg


def _parse_naming_clip(raw: dict, source: str) -> NamingClipConfig:
    cfg = NamingClipConfig()
    if "include" in raw:
        include = _require_dict(raw["include"], "naming.clip.include", source)
        for key, val in include.items():
            if key not in KNOWN_CLIP_COMPONENTS:
                raise ConfigLoadError(
                    f"{source}: unknown naming.clip.include key {key!r}. "
                    f"Known components: {', '.join(KNOWN_CLIP_COMPONENTS)}"
                )
            cfg.include[key] = _require_bool(val, f"naming.clip.include.{key}", source)
    if "order" in raw:
        order = _require_list(raw["order"], "naming.clip.order", source)
        for item in order:
            if not isinstance(item, str) or item not in KNOWN_CLIP_COMPONENTS:
                raise ConfigLoadError(
                    f"{source}: unknown naming.clip.order item {item!r}. "
                    f"Known components: {', '.join(KNOWN_CLIP_COMPONENTS)}"
                )
        cfg.order = list(order)
    if "separator" in raw:
        cfg.separator = _require_str(raw["separator"], "naming.clip.separator", source)
    if "index_format" in raw:
        cfg.index_format = _require_str(raw["index_format"], "naming.clip.index_format", source)
    if not any(cfg.include.get(c, False) for c in cfg.order):
        raise ConfigLoadError(
            f"{source}: naming.clip requires at least one include component to be true"
        )
    return cfg


def _parse_naming(raw: dict, source: str) -> NamingConfig:
    cfg = NamingConfig()
    if "dir" in raw:
        d = _require_dict(raw["dir"], "naming.dir", source)
        cfg.dir = _parse_naming_dir(d, source)
    if "clip" in raw:
        d = _require_dict(raw["clip"], "naming.clip", source)
        cfg.clip = _parse_naming_clip(d, source)
    return cfg


def _parse_defaults(raw: dict, source: str) -> DefaultsConfig:
    cfg = DefaultsConfig()
    if "detector" in raw:
        cfg.detector = _require_str(raw["detector"], "defaults.detector", source)
    if "candidates" in raw:
        cfg.candidates = _require_int(raw["candidates"], "defaults.candidates", source)
    if "window" in raw:
        cfg.window = _require_number(raw["window"], "defaults.window", source)
    if "min_duration" in raw:
        cfg.min_duration = _require_number(raw["min_duration"], "defaults.min_duration", source)
    if "max_duration" in raw:
        cfg.max_duration = _require_number(raw["max_duration"], "defaults.max_duration", source)
    if "export_clips" in raw:
        cfg.export_clips = _require_bool(raw["export_clips"], "defaults.export_clips", source)
    if "export_thumbnails" in raw:
        cfg.export_thumbnails = _require_bool(
            raw["export_thumbnails"], "defaults.export_thumbnails", source
        )
    if "debug" in raw:
        cfg.debug = _require_bool(raw["debug"], "defaults.debug", source)
    if "weights" in raw and raw["weights"] is not None:
        cfg.weights = _require_dict(raw["weights"], "defaults.weights", source)
    return cfg


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def parse_config(raw: dict, *, source: str = "<inline>") -> Config:
    """Validate a raw config dict and return a fully-populated ``Config``."""
    if not isinstance(raw, dict):
        raise ConfigLoadError(f"{source}: top-level must be a mapping")
    cfg = Config()
    if "paths" in raw:
        d = _require_dict(raw["paths"], "paths", source)
        cfg.paths = _parse_paths(d, source)
    if "naming" in raw:
        d = _require_dict(raw["naming"], "naming", source)
        cfg.naming = _parse_naming(d, source)
    if "defaults" in raw:
        d = _require_dict(raw["defaults"], "defaults", source)
        cfg.defaults = _parse_defaults(d, source)
    return cfg


def load_config(path: Path) -> Config:
    """Read ``path`` and return a ``Config``.

    Missing file raises ``ConfigLoadError`` rather than ``FileNotFoundError``
    so callers only need to catch one exception type.
    """
    if not path.exists():
        raise ConfigLoadError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigLoadError(f"invalid YAML in {path}: {e}") from e
    if raw is None:
        raw = {}  # empty file → all defaults
    cfg = parse_config(raw, source=str(path))
    cfg.source_path = path
    return cfg
