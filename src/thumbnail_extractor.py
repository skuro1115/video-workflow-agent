"""Extract one representative still per planned clip via ffmpeg.

Off by default — pass ``--export-thumbnails`` (or set
``"export_thumbnails": true`` in ``settings.json``) to enable.

The motivation: ``clip_plan.json`` is the file humans review before paying
the cost of a re-encode, but a JSON list of timestamps and reasons is hard
to skim visually. A folder of midpoint frames is much faster — the reviewer
can spot the wrong window in seconds and edit the plan before
``--from-plan`` does the slow encode.

Cost: one ffmpeg invocation per clip, each grabbing a single frame. ~50 ms
per clip for 1080p, dwarfed by everything else in the pipeline.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .clip_planner import ClipPlan


class ThumbnailExtractionError(RuntimeError):
    """Raised when ffmpeg is missing on PATH for thumbnail extraction."""


def _ensure_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise ThumbnailExtractionError(
            "ffmpeg not found on PATH. Install ffmpeg "
            "(macOS: `brew install ffmpeg`, Ubuntu: `apt install ffmpeg`) "
            "or re-run without --export-thumbnails."
        )
    return path


def extract_thumbnails(
    *,
    input_path: Path,
    output_dir: Path,
    plans: list[ClipPlan],
    time_offset_ratio: float = 0.5,
    quality: int = 2,
) -> list[dict]:
    """Grab one frame per clip and write to ``<output_dir>/<clip_id>.jpg``.

    The frame timestamp is
    ``source_start + (source_end - source_start) * time_offset_ratio``.
    Default 0.5 = visual midpoint, the best single summary frame for a
    typical 30s window. Set to 0.0 for the very first frame (useful when
    the clip's start *is* the punchline).

    ``quality`` is ffmpeg's ``-q:v`` (1=best, 31=worst). 2 is visually
    near-lossless and small (~50–150 KB at 1080p).

    Per-clip failures are isolated — one bad seek doesn't kill the rest.
    Returns a status entry per plan, mirroring ``clip_export_result``.
    """
    ffmpeg = _ensure_ffmpeg()
    output_dir.mkdir(parents=True, exist_ok=True)
    # Clamp ratio defensively — out-of-range would point ffmpeg outside the clip.
    ratio = max(0.0, min(1.0, time_offset_ratio))
    results: list[dict] = []

    for plan in plans:
        t = plan.source_start + (plan.source_end - plan.source_start) * ratio
        out_path = output_dir / f"{plan.clip_id}.jpg"
        cmd = [
            ffmpeg,
            "-y",
            # -ss before -i = fast keyframe seek. For a single still we never
            # need frame-accurate decode (sub-second imprecision is invisible
            # in a thumbnail), and this is ~100x faster than -ss after -i.
            "-ss", f"{t}",
            "-i", str(input_path),
            "-frames:v", "1",
            "-q:v", str(quality),
            # -update 1 tells ffmpeg the output path is a single image, not
            # a sequence pattern — silences the "image2 needs %d" warning.
            "-update", "1",
            str(out_path),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            results.append({
                "clip_id": plan.clip_id,
                "status": "extracted",
                "path": str(out_path),
                "t": round(t, 3),
            })
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip().splitlines()
            results.append({
                "clip_id": plan.clip_id,
                "status": "failed",
                "error": stderr[-1] if stderr else "unknown ffmpeg error",
            })
    return results
