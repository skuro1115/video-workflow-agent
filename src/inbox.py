"""Process ``*.task.yaml`` files from an inbox directory.

The primary user-facing workflow for non-engineers and AI agents:

    inbox/foo.task.yaml  →  process-inbox  →  output/<rendered>/ + archive/foo.task.yaml

A task descriptor carries:

    source:   ./samples/sample.mp4   # or https://… URL (required)
    streamer: streamerA              # used by naming.dir.include.streamer
    purpose:  funny                  # used by naming.dir.include.purpose
    title:    "【神回】…"           # used by naming.dir.include.title + clip.slug
    date:     2026-05-15             # optional ISO date (else today)
    chat_log: ./samples/foo.chat.json   # optional; URL sources auto-fetch chat

    # Any defaults.* field can be overridden per-task:
    detector: composite
    candidates: 10
    window: 30
    weights: {...}                   # raw — same shape as config defaults.weights

Lifecycle:

  * success → ``archive/<task>.task.yaml``
  * failure → ``failed/<task>.task.yaml`` + ``failed/<task>.error.log``

Paths in ``config.yaml`` are resolved relative to the config file's
directory (not CWD) so the workflow is location-independent.
"""
from __future__ import annotations

import datetime as _dt
import shutil
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config_loader import Config, ConfigLoadError
from .naming import ClipComponents, DirComponents, render_clip_name, resolve_output_dir


class TaskLoadError(ValueError):
    """Raised when a task file is missing required fields or malformed."""


# Keys recognised in *.task.yaml. Unknown keys raise — typos shouldn't be
# silently ignored (e.g. "streemer" should fail loudly so the user fixes it
# instead of getting an output directory missing the streamer component).
_TASK_KNOWN_KEYS: frozenset[str] = frozenset({
    "source", "streamer", "purpose", "title", "date", "chat_log",
    # per-task overrides (mirror defaults block):
    "detector", "candidates", "window", "min_duration", "max_duration",
    "export_clips", "export_thumbnails", "debug", "weights",
})


@dataclass
class Task:
    """A parsed ``*.task.yaml``."""

    name: str                    # task file stem (used as the ``task`` naming component)
    source_path: Path            # absolute path on disk to the .task.yaml
    source: str                  # URL or local path string (required)
    streamer: str | None = None
    purpose: str | None = None
    title: str | None = None
    date: _dt.date | None = None
    chat_log: str | None = None
    # Per-task overrides — same key names as DefaultsConfig fields. Unknown
    # keys never land here; they raise during parse.
    overrides: dict[str, Any] = field(default_factory=dict)


def parse_task(path: Path) -> Task:
    """Parse one task file. Raises ``TaskLoadError`` on any structural problem."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise TaskLoadError(f"invalid YAML in {path}: {e}") from e
    except OSError as e:
        raise TaskLoadError(f"could not read {path}: {e}") from e
    if raw is None:
        raise TaskLoadError(f"{path}: task file is empty")
    if not isinstance(raw, dict):
        raise TaskLoadError(f"{path}: top-level must be a mapping")

    unknown = set(raw.keys()) - _TASK_KNOWN_KEYS
    if unknown:
        raise TaskLoadError(
            f"{path}: unknown task keys: {sorted(unknown)}. "
            f"Known: {sorted(_TASK_KNOWN_KEYS)}"
        )

    source = raw.get("source")
    if not isinstance(source, str) or not source.strip():
        raise TaskLoadError(f"{path}: 'source' is required (URL or local path)")

    date_val: _dt.date | None = None
    if "date" in raw and raw["date"] is not None:
        d = raw["date"]
        # PyYAML parses ``2026-05-15`` as a datetime.date already.
        if isinstance(d, _dt.date):
            date_val = d
        elif isinstance(d, str):
            try:
                date_val = _dt.date.fromisoformat(d)
            except ValueError as e:
                raise TaskLoadError(
                    f"{path}: 'date' must be ISO format YYYY-MM-DD, got {d!r}"
                ) from e
        else:
            raise TaskLoadError(
                f"{path}: 'date' must be a string or date, got {type(d).__name__}"
            )

    # Strings that may be missing.
    def _opt_str(key: str) -> str | None:
        v = raw.get(key)
        if v is None:
            return None
        if not isinstance(v, str):
            raise TaskLoadError(f"{path}: '{key}' must be a string, got {type(v).__name__}")
        return v

    # Pull out anything that overrides a defaults.* field, so the inbox
    # processor can apply them on top of the config.
    overrides: dict[str, Any] = {}
    for key in (
        "detector", "candidates", "window", "min_duration", "max_duration",
        "export_clips", "export_thumbnails", "debug", "weights",
    ):
        if key in raw:
            overrides[key] = raw[key]

    return Task(
        name=path.stem.removesuffix(".task") if path.stem.endswith(".task") else path.stem,
        source_path=path,
        source=source.strip(),
        streamer=_opt_str("streamer"),
        purpose=_opt_str("purpose"),
        title=_opt_str("title"),
        date=date_val,
        chat_log=_opt_str("chat_log"),
        overrides=overrides,
    )


def discover_tasks(inbox_dir: Path) -> list[Path]:
    """Return ``*.task.yaml`` paths in ``inbox_dir``, sorted by filename.

    Sorted so the order is deterministic (helpful for AI agents picking the
    first available task, and for reproducible CI runs).
    """
    if not inbox_dir.exists():
        return []
    paths = sorted(inbox_dir.glob("*.task.yaml"))
    # Also pick up *.task.yml as a courtesy.
    paths.extend(sorted(inbox_dir.glob("*.task.yml")))
    return paths


# ---------------------------------------------------------------------------
# Path resolution: config paths are relative to the config file's directory.
# ---------------------------------------------------------------------------

def resolve_relative_to_config(cfg: Config, p: Path) -> Path:
    """Resolve a config-relative path against the config file's directory.

    If the config wasn't loaded from a file (e.g. parsed inline from a test),
    resolve against CWD. Absolute paths pass through unchanged.
    """
    if p.is_absolute():
        return p
    base = cfg.source_path.parent if cfg.source_path is not None else Path.cwd()
    return (base / p).resolve()


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

def _is_url(source: str) -> bool:
    return source.startswith(("http://", "https://"))


@dataclass
class TaskResult:
    """Outcome of processing one task."""

    task_name: str
    status: str                   # "success" | "failed" | "skipped"
    output_dir: Path | None = None
    error: str | None = None      # one-line summary
    error_detail: str | None = None  # full traceback for the failure log


def _suffixed_name(filename: str, n: int) -> str:
    """Insert ``_<n>`` before the extension(s).

    Handles the compound ``.task.yaml`` / ``.task.yml`` suffixes so a re-run
    produces ``foo_2.task.yaml`` rather than the ugly ``foo.task_2.yaml`` you
    get from naive ``Path.stem`` splitting.
    """
    for compound in (".task.yaml", ".task.yml"):
        if filename.endswith(compound):
            return f"{filename[: -len(compound)]}_{n}{compound}"
    p = Path(filename)
    return f"{p.stem}_{n}{p.suffix}"


def _archive_task(task_path: Path, archive_dir: Path) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / task_path.name
    n = 2
    while target.exists():
        target = archive_dir / _suffixed_name(task_path.name, n)
        n += 1
    shutil.move(str(task_path), str(target))
    return target


def _move_to_failed(task_path: Path, failed_dir: Path, error_text: str) -> Path:
    failed_dir.mkdir(parents=True, exist_ok=True)
    target = failed_dir / task_path.name
    n = 2
    while target.exists():
        target = failed_dir / _suffixed_name(task_path.name, n)
        n += 1
    shutil.move(str(task_path), str(target))
    log_path = target.with_name(target.name + ".error.log")
    log_path.write_text(error_text, encoding="utf-8")
    return target


def _build_pipeline_config(
    cfg: Config,
    task: Task,
    *,
    input_path: Path,
    output_dir: Path,
):
    """Build the inner-pipeline ``PipelineConfig`` from defaults + task overrides.

    Imported lazily to avoid a hard dep on src.config at module top level —
    keeps test fixtures lighter and matches the project's "minimise core
    imports" convention.
    """
    from .config import PipelineConfig

    o = task.overrides
    return PipelineConfig(
        input_path=input_path,
        output_dir=output_dir,
        detector=o.get("detector", cfg.defaults.detector),
        candidate_count=int(o.get("candidates", cfg.defaults.candidates)),
        candidate_duration=float(o.get("window", cfg.defaults.window)),
        min_clip_duration=float(o.get("min_duration", cfg.defaults.min_duration)),
        max_clip_duration=float(o.get("max_duration", cfg.defaults.max_duration)),
        export_clips=bool(o.get("export_clips", cfg.defaults.export_clips)),
        export_thumbnails=bool(o.get("export_thumbnails", cfg.defaults.export_thumbnails)),
    )


def _resolve_weights(cfg: Config, task: Task):
    """Merge defaults.weights with task overrides.weights. Returns Weights or None."""
    from .score_weights import WeightsConfigError, parse_weights_dict

    raw = task.overrides.get("weights", cfg.defaults.weights)
    if raw is None:
        return None
    try:
        return parse_weights_dict(raw, source=str(task.source_path))
    except WeightsConfigError as e:
        raise TaskLoadError(f"{task.source_path}: invalid weights: {e}") from e


def process_task(
    cfg: Config,
    task: Task,
    *,
    fetch_cache_dir: Path | None = None,
) -> TaskResult:
    """Run the pipeline for one task. Doesn't touch the inbox/archive itself —
    that's the caller's job, so dry-runs and tests can reuse this.

    Returns a ``TaskResult``; never raises (errors are captured into the result).
    """
    from .main import run as _run

    try:
        # 1. Resolve input source.
        if _is_url(task.source):
            if fetch_cache_dir is None:
                fetch_cache_dir = resolve_relative_to_config(cfg, Path("./_cache"))
            fetch_cache_dir.mkdir(parents=True, exist_ok=True)
            from scripts import fetch as _fetch
            try:
                video, chat = _fetch.fetch(task.source, fetch_cache_dir, task.name)
            except (_fetch.FetchError, ValueError) as e:
                raise RuntimeError(f"fetch failed: {e}") from e
            input_path = video
            chat_log_path: Path | None = chat
        else:
            input_path = resolve_relative_to_config(cfg, Path(task.source))
            if not input_path.exists():
                raise FileNotFoundError(f"source video not found: {input_path}")
            chat_log_path = (
                resolve_relative_to_config(cfg, Path(task.chat_log))
                if task.chat_log else None
            )

        # 2. Compute output dir (with naming + conflict resolution).
        output_root = resolve_relative_to_config(cfg, cfg.paths.output)
        output_root.mkdir(parents=True, exist_ok=True)
        comps = DirComponents(
            date=task.date or _dt.date.today(),
            streamer=task.streamer,
            purpose=task.purpose,
            title=task.title,
            detector=task.overrides.get("detector", cfg.defaults.detector),
            task=task.name,
        )
        output_dir = resolve_output_dir(cfg.naming.dir, comps, output_root)

        # 3. Build pipeline config + weights, then run.
        pipeline_cfg = _build_pipeline_config(
            cfg, task, input_path=input_path, output_dir=output_dir,
        )
        weights = _resolve_weights(cfg, task)
        debug = bool(task.overrides.get("debug", cfg.defaults.debug))

        rc = _run(
            pipeline_cfg,
            from_plan=None,
            debug=debug,
            chat_log_path=chat_log_path,
            weights=weights,
        )
        if rc != 0:
            raise RuntimeError(f"pipeline returned non-zero exit code: {rc}")

        # 4. Rename clip files to the configured pattern if any were exported.
        _rename_clips_in_place(cfg, task, output_dir)

        return TaskResult(
            task_name=task.name,
            status="success",
            output_dir=output_dir,
        )
    except Exception as e:
        return TaskResult(
            task_name=task.name,
            status="failed",
            error=str(e) or e.__class__.__name__,
            error_detail=traceback.format_exc(),
        )


def _rename_clips_in_place(cfg: Config, task: Task, output_dir: Path) -> None:
    """Rename ``output_dir/clips/*.mp4`` + matching thumbnails per ``naming.clip``.

    The exporter writes ``<clip_id>.mp4`` by default (e.g. ``clip_01.mp4``).
    We map each to the configured naming pattern using the planned clip's
    start time + the task's title as the slug source. Best-effort: failures
    here only log a warning so the rest of the run still counts as success.
    """
    import json

    plan_path = output_dir / "clip_plan.json"
    if not plan_path.exists():
        return
    try:
        plans = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    clips_dir = output_dir / "clips"
    thumbs_dir = output_dir / "thumbnails"
    detector_name = task.overrides.get("detector", cfg.defaults.detector)

    for i, entry in enumerate(plans, start=1):
        clip_id = entry.get("clip_id")
        if not clip_id:
            continue
        new_stem = render_clip_name(
            cfg.naming.clip,
            ClipComponents(
                index=i,
                slug=task.title or task.purpose or clip_id,
                detector=detector_name,
                timestamp_seconds=float(entry.get("source_start", 0)),
            ),
        )
        if new_stem == clip_id:
            continue
        for d, ext in ((clips_dir, ".mp4"), (thumbs_dir, ".jpg")):
            src = d / f"{clip_id}{ext}"
            if src.exists():
                target = d / f"{new_stem}{ext}"
                # Avoid clobbering an unrelated file that happens to share the name.
                if not target.exists():
                    try:
                        src.rename(target)
                    except OSError as e:
                        print(
                            f"WARNING: could not rename {src} → {target}: {e}",
                            file=sys.stderr,
                        )


@dataclass
class InboxResult:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    results: list[TaskResult] = field(default_factory=list)


def process_inbox(
    cfg: Config,
    *,
    task_name: str | None = None,
    dry_run: bool = False,
) -> InboxResult:
    """Discover + process every ``*.task.yaml`` in ``cfg.paths.inbox``.

    Args:
        task_name: if given, process only the task with this stem (no
            ``.task.yaml`` suffix). Useful for targeted re-runs.
        dry_run: parse + report but don't run the pipeline or move files.

    Returns an ``InboxResult`` summarising what happened. The caller decides
    how to surface this (CLI prints, MCP returns, etc.).
    """
    inbox_dir = resolve_relative_to_config(cfg, cfg.paths.inbox)
    archive_dir = resolve_relative_to_config(cfg, cfg.paths.archive)
    failed_dir = resolve_relative_to_config(cfg, cfg.paths.failed)

    paths = discover_tasks(inbox_dir)
    if task_name is not None:
        paths = [p for p in paths if p.stem.removesuffix(".task") == task_name
                 or p.stem == task_name]
        if not paths:
            print(
                f"INFO: no task matching {task_name!r} in {inbox_dir}",
                file=sys.stderr,
            )

    result = InboxResult(total=len(paths))

    for p in paths:
        try:
            task = parse_task(p)
        except TaskLoadError as e:
            print(f"FAILED {p.name}: {e}", file=sys.stderr)
            tr = TaskResult(
                task_name=p.stem, status="failed",
                error=str(e), error_detail=str(e),
            )
            result.results.append(tr)
            result.failed += 1
            if not dry_run:
                _move_to_failed(p, failed_dir, str(e))
            continue

        if dry_run:
            print(f"[dry-run] would process {p.name} → "
                  f"streamer={task.streamer} purpose={task.purpose}")
            result.results.append(TaskResult(task_name=task.name, status="skipped"))
            result.skipped += 1
            continue

        print(f"[inbox] processing {p.name}")
        tr = process_task(cfg, task)
        result.results.append(tr)
        if tr.status == "success":
            print(f"        → {tr.output_dir}")
            _archive_task(p, archive_dir)
            result.succeeded += 1
        else:
            print(f"        FAILED: {tr.error}", file=sys.stderr)
            _move_to_failed(p, failed_dir, tr.error_detail or tr.error or "unknown error")
            result.failed += 1

    return result


def load_config_or_default(path: Path | None) -> Config:
    """Resolve the config path or fall back to ``./config.yaml`` in CWD.

    Returns a default Config (with no source_path) if neither is present —
    that lets process-inbox run with no config file at all, using built-in
    defaults. Useful for the absolute-minimum first-run experience.
    """
    from .config_loader import Config, load_config

    if path is not None:
        return load_config(path)
    default_path = Path.cwd() / "config.yaml"
    if default_path.exists():
        return load_config(default_path)
    return Config()
