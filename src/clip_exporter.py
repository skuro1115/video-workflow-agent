"""Cut clips with ffmpeg based on a list of ClipPlan entries.

Off by default in the pipeline — you must pass `--export-clips` to actually
encode. The first run usually only writes the plan JSON so a human can review
before any encoding cost is paid.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .clip_planner import ClipPlan


class FFmpegNotFoundError(RuntimeError):
    """Raised when `ffmpeg` is missing on PATH."""


def _ensure_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise FFmpegNotFoundError(
            "ffmpeg not found on PATH. Install ffmpeg "
            "(macOS: `brew install ffmpeg`, Ubuntu: `apt install ffmpeg`) "
            "or re-run without --export-clips."
        )
    return path


def export_clips(
    *,
    input_path: Path,
    output_dir: Path,
    plans: list[ClipPlan],
    video_codec: str = "libx264",
    audio_codec: str = "aac",
) -> list[dict]:
    """Cut each planned clip via ffmpeg, returning a status entry per clip.

    Re-encodes (rather than stream-copy) so cuts land on exact frames. Switch to
    `-c copy` later if speed matters more than precision.
    """
    ffmpeg = _ensure_ffmpeg()
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []

    for plan in plans:
        out_path = output_dir / f"{plan.clip_id}.mp4"
        cmd = [
            ffmpeg,
            "-y",
            "-ss", f"{plan.source_start}",
            "-i", str(input_path),
            "-t", f"{plan.duration}",
            "-c:v", video_codec,
            "-c:a", audio_codec,
            "-movflags", "+faststart",
            str(out_path),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            results.append({
                "clip_id": plan.clip_id,
                "status": "exported",
                "path": str(out_path),
            })
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip().splitlines()
            results.append({
                "clip_id": plan.clip_id,
                "status": "failed",
                "error": stderr[-1] if stderr else "unknown ffmpeg error",
            })
    return results
