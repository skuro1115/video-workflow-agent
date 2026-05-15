"""Naming engine for the inbox workflow.

Pure functions only — no I/O except the conflict-resolution path-exists check.
The engine turns a task's metadata + a ``NamingDirConfig`` / ``NamingClipConfig``
into:

  * an output directory name (``render_dir_name`` / ``resolve_output_dir``)
  * a clip filename stem  (``render_clip_name``)

Both rely on the same idea: walk ``order``, emit only components for which
``include[component] == True``, join with ``separator``. A component value
that resolves to an empty string is silently skipped (so a task without a
``streamer`` doesn't produce ``2026-05-15__funny``).

The conflict resolution logic lives here too because it's tightly coupled
to the dir-name computation, and keeping it pure-ish (only ``Path.exists()``
is touched) keeps the rest of the codebase free of name-mangling.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path

from .config_loader import NamingClipConfig, NamingDirConfig


# Filesystem-unsafe characters across macOS / Linux / Windows. We replace them
# with '-' rather than dropping so a slug stays roughly the same length and
# the boundary between words is preserved.
_UNSAFE_CHARS = re.compile(r'[\\/:\*\?"<>\|\x00-\x1f]')
# Whitespace + repeated '-' / '_' are collapsed into a single '-'.
_COLLAPSE = re.compile(r"[\s\-_]+")


def slugify(text: str, max_length: int = 40) -> str:
    """Turn arbitrary text into a filesystem-safe slug.

    Rules:
      - Replace path-unsafe chars (``\\ / : * ? " < > |`` + control bytes) with ``-``.
      - Collapse runs of whitespace, ``-``, ``_`` into a single ``-``.
      - Strip leading/trailing ``-``.
      - Truncate to ``max_length`` characters (no word-boundary cleverness —
        simple truncate is predictable).
      - Empty result → ``"untitled"``.

    Japanese / Korean / emoji are kept as-is. macOS HFS+/APFS and Linux ext4
    both handle Unicode filenames fine; Windows users hitting issues should
    rename via configuration.
    """
    if not text:
        return "untitled"
    s = _UNSAFE_CHARS.sub("-", text)
    s = _COLLAPSE.sub("-", s)
    s = s.strip("-")
    if max_length > 0 and len(s) > max_length:
        s = s[:max_length].rstrip("-")
    return s or "untitled"


@dataclass
class DirComponents:
    """The set of values a task contributes to its output directory name.

    Field names match the ``naming.dir.include`` keys verbatim so the engine
    can look them up by component name. ``date`` is a ``datetime.date`` so the
    config's ``date_format`` strftime template can be applied; everything else
    is a string (or ``None`` if the task didn't supply it).
    """

    date: _date | None = None
    streamer: str | None = None
    purpose: str | None = None
    title: str | None = None
    detector: str | None = None
    task: str | None = None


def render_dir_name(cfg: NamingDirConfig, comps: DirComponents) -> str:
    """Build the directory name string (no path, no conflict resolution).

    Walks ``cfg.order``; for each component, emits its rendered value only if
    ``cfg.include[component]`` is true AND the value resolves to non-empty.
    Components missing from ``cfg.order`` are never emitted, even if their
    ``include`` flag is on (the order list is the source of truth for what
    appears in the output).
    """
    parts: list[str] = []
    for component in cfg.order:
        if not cfg.include.get(component, False):
            continue
        rendered = _render_component(component, comps, cfg)
        if rendered:
            parts.append(rendered)
    if not parts:
        raise ValueError(
            "naming produced an empty directory name — "
            "the task is missing values for every enabled component"
        )
    return cfg.separator.join(parts)


def _render_component(component: str, comps: DirComponents, cfg: NamingDirConfig) -> str:
    """Render one component to a string. Empty → component is skipped."""
    if component == "date":
        d = comps.date or _date.today()
        return d.strftime(cfg.date_format)
    if component == "streamer":
        return slugify(comps.streamer or "", cfg.slug_max_length) if comps.streamer else ""
    if component == "purpose":
        return slugify(comps.purpose or "", cfg.slug_max_length) if comps.purpose else ""
    if component == "title":
        return slugify(comps.title or "", cfg.slug_max_length) if comps.title else ""
    if component == "detector":
        return comps.detector or ""
    if component == "task":
        # Task stem is already a filename, so it's mostly safe — but still
        # slugify it (in case someone puts spaces in their .task.yaml name).
        return slugify(comps.task or "", cfg.slug_max_length) if comps.task else ""
    # Should be caught by config validation, but fail loudly if a new
    # component is added to KNOWN_DIR_COMPONENTS without updating this fn.
    raise ValueError(f"unhandled naming.dir component: {component!r}")


def resolve_output_dir(
    cfg: NamingDirConfig,
    comps: DirComponents,
    output_root: Path,
) -> Path:
    """Compute and disambiguate the output directory path.

    Conflict policy:
      - ``cfg.on_conflict == "suffix"`` (default): if ``base`` exists, try
        ``base_2``, ``base_3``, … until one is free.
      - ``cfg.on_conflict == "error"``: raise ``FileExistsError``.

    When ``include.task`` is true, conflicts are theoretically impossible
    (task stems are unique within the inbox). The suffix path is still a
    safety net for the rare re-run case.
    """
    base_name = render_dir_name(cfg, comps)
    base = output_root / base_name
    if not base.exists():
        return base
    if cfg.on_conflict == "error":
        raise FileExistsError(
            f"output directory already exists: {base} "
            f"(naming.dir.on_conflict=error)"
        )
    # suffix mode
    for n in range(2, 1000):  # cap so we don't loop forever on a weird FS
        candidate = output_root / f"{base_name}{cfg.separator}{n}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(
        f"could not find a free suffix for {base_name} after 999 attempts"
    )


@dataclass
class ClipComponents:
    """Values used to render a clip filename stem."""

    index: int | None = None
    slug: str | None = None
    detector: str | None = None
    timestamp_seconds: float | None = None


def render_clip_name(cfg: NamingClipConfig, comps: ClipComponents) -> str:
    """Build the clip filename stem (no extension, no path).

    Same walk-and-emit rule as ``render_dir_name``.
    """
    parts: list[str] = []
    for component in cfg.order:
        if not cfg.include.get(component, False):
            continue
        rendered = _render_clip_component(component, comps, cfg)
        if rendered:
            parts.append(rendered)
    if not parts:
        raise ValueError(
            "naming produced an empty clip name — the clip is missing "
            "values for every enabled component"
        )
    return cfg.separator.join(parts)


def _render_clip_component(
    component: str, comps: ClipComponents, cfg: NamingClipConfig
) -> str:
    if component == "index":
        if comps.index is None:
            return ""
        try:
            return cfg.index_format.format(comps.index)
        except (IndexError, ValueError) as e:
            raise ValueError(
                f"index_format {cfg.index_format!r} is not a valid Python "
                f"format string for index={comps.index}: {e}"
            )
    if component == "slug":
        return slugify(comps.slug or "", max_length=40) if comps.slug else ""
    if component == "detector":
        return comps.detector or ""
    if component == "timestamp":
        if comps.timestamp_seconds is None:
            return ""
        return _format_timestamp(comps.timestamp_seconds)
    raise ValueError(f"unhandled naming.clip component: {component!r}")


def _format_timestamp(seconds: float) -> str:
    """Format ``seconds`` as ``MMmSSs`` or ``HHhMMmSSs`` for clip filenames.

    Filename-safe and short. ``3725`` → ``1h02m05s``; ``45`` → ``00m45s``.
    """
    s = int(max(0.0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h > 0:
        return f"{h}h{m:02d}m{sec:02d}s"
    return f"{m:02d}m{sec:02d}s"
