"""Extract video metadata via ffprobe.

Shells out to `ffprobe` rather than depending on a Python binding, so the
runtime requirement is just a working ffmpeg install on PATH.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


class FFprobeNotFoundError(RuntimeError):
    """Raised when `ffprobe` is missing on PATH."""


class FFprobeFailedError(RuntimeError):
    """Raised when ffprobe runs but exits non-zero."""


@dataclass
class VideoInfo:
    path: str
    duration: float          # seconds
    width: int | None
    height: int | None
    fps: float | None
    video_codec: str | None
    audio_codec: str | None
    bit_rate: int | None
    container: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _ensure_ffprobe() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise FFprobeNotFoundError(
            "ffprobe not found on PATH. Install ffmpeg "
            "(macOS: `brew install ffmpeg`, Ubuntu: `apt install ffmpeg`) "
            "and re-run."
        )
    return path


def _parse_fps(stream: dict) -> float | None:
    rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    if not rate or "/" not in rate:
        return None
    num, den = rate.split("/", 1)
    try:
        num_f = float(num)
        den_f = float(den)
    except ValueError:
        return None
    return num_f / den_f if den_f else None


def probe(input_path: Path) -> VideoInfo:
    """Run ffprobe and return a VideoInfo for the file."""
    if not input_path.exists():
        raise FileNotFoundError(f"Input video not found: {input_path}")

    ffprobe = _ensure_ffprobe()
    cmd = [
        ffprobe,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(input_path),
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise FFprobeFailedError(
            f"ffprobe failed for {input_path}: {stderr or 'no stderr'}"
        ) from exc

    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = 0.0
    if fmt.get("duration"):
        try:
            duration = float(fmt["duration"])
        except ValueError:
            duration = 0.0

    bit_rate: int | None = None
    raw_br = fmt.get("bit_rate")
    if isinstance(raw_br, str) and raw_br.isdigit():
        bit_rate = int(raw_br)

    return VideoInfo(
        path=str(input_path),
        duration=duration,
        width=int(video_stream["width"]) if video_stream and "width" in video_stream else None,
        height=int(video_stream["height"]) if video_stream and "height" in video_stream else None,
        fps=_parse_fps(video_stream) if video_stream else None,
        video_codec=video_stream.get("codec_name") if video_stream else None,
        audio_codec=audio_stream.get("codec_name") if audio_stream else None,
        bit_rate=bit_rate,
        container=fmt.get("format_name"),
    )
