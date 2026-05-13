"""Pipeline configuration.

Centralises paths and tuning knobs so the rest of the pipeline does not
hard-code numbers. The CLI in `main.py` is the single source for overrides.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class PipelineConfig:
    input_path: Path
    output_dir: Path

    # Hotspot detection
    detector: str = "even"
    candidate_count: int = 6
    candidate_duration: float = 30.0  # seconds per candidate window

    # Clip planning
    min_clip_duration: float = 10.0
    max_clip_duration: float = 60.0

    # Export
    export_clips: bool = False
    export_thumbnails: bool = False
    video_codec: str = "libx264"
    audio_codec: str = "aac"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["input_path"] = str(self.input_path)
        d["output_dir"] = str(self.output_dir)
        return d
