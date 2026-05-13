"""Tests for the thumbnail extractor.

Mocks ``subprocess.run`` so the suite stays ffmpeg-free, matching the
project's no-external-dependency test convention.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.clip_planner import ClipPlan
from src.thumbnail_extractor import (
    ThumbnailExtractionError,
    extract_thumbnails,
)


def _plan(clip_id: str, start: float, end: float) -> ClipPlan:
    return ClipPlan(
        clip_id=clip_id,
        source_start=start,
        source_end=end,
        duration=end - start,
        purpose="short clip candidate",
        status="planned",
        score=0.5,
        reason="test",
    )


class ThumbnailExtractorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_midpoint_is_default_offset(self) -> None:
        # Plan from 10s to 30s → midpoint = 20s. The first ffmpeg call
        # should pass `-ss 20` for that clip.
        plans = [_plan("clip_001", 10.0, 30.0)]
        with patch("src.thumbnail_extractor.subprocess.run") as run, \
             patch("src.thumbnail_extractor.shutil.which", return_value="/usr/bin/ffmpeg"):
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            results = extract_thumbnails(
                input_path=Path("video.mp4"),
                output_dir=self.tmp_path,
                plans=plans,
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "extracted")
        self.assertEqual(results[0]["t"], 20.0)
        # Verify ffmpeg got -ss 20.0 (midpoint of [10, 30]).
        cmd = run.call_args.args[0]
        ss_idx = cmd.index("-ss")
        self.assertEqual(float(cmd[ss_idx + 1]), 20.0)

    def test_offset_ratio_zero_picks_clip_start(self) -> None:
        plans = [_plan("clip_001", 100.0, 200.0)]
        with patch("src.thumbnail_extractor.subprocess.run") as run, \
             patch("src.thumbnail_extractor.shutil.which", return_value="/usr/bin/ffmpeg"):
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            results = extract_thumbnails(
                input_path=Path("video.mp4"),
                output_dir=self.tmp_path,
                plans=plans,
                time_offset_ratio=0.0,
            )
        self.assertEqual(results[0]["t"], 100.0)
        cmd = run.call_args.args[0]
        ss_idx = cmd.index("-ss")
        self.assertEqual(float(cmd[ss_idx + 1]), 100.0)

    def test_ratio_clamped_to_unit_interval(self) -> None:
        plans = [_plan("clip_001", 50.0, 60.0)]
        with patch("src.thumbnail_extractor.subprocess.run") as run, \
             patch("src.thumbnail_extractor.shutil.which", return_value="/usr/bin/ffmpeg"):
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            # Ratio > 1.0 should be clamped to 1.0 (= source_end).
            results = extract_thumbnails(
                input_path=Path("video.mp4"),
                output_dir=self.tmp_path,
                plans=plans,
                time_offset_ratio=2.5,
            )
        self.assertEqual(results[0]["t"], 60.0)

    def test_per_clip_failure_isolated(self) -> None:
        plans = [
            _plan("clip_001", 0.0, 10.0),
            _plan("clip_002", 20.0, 30.0),
            _plan("clip_003", 40.0, 50.0),
        ]
        # Second invocation fails; first and third succeed.
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        fail = subprocess.CalledProcessError(
            returncode=1, cmd=[], output="", stderr="ffmpeg: invalid timestamp\n",
        )
        with patch("src.thumbnail_extractor.subprocess.run", side_effect=[ok, fail, ok]), \
             patch("src.thumbnail_extractor.shutil.which", return_value="/usr/bin/ffmpeg"):
            results = extract_thumbnails(
                input_path=Path("video.mp4"),
                output_dir=self.tmp_path,
                plans=plans,
            )
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["status"], "extracted")
        self.assertEqual(results[1]["status"], "failed")
        self.assertIn("invalid timestamp", results[1]["error"])
        self.assertEqual(results[2]["status"], "extracted")

    def test_missing_ffmpeg_raises(self) -> None:
        with patch("src.thumbnail_extractor.shutil.which", return_value=None):
            with self.assertRaises(ThumbnailExtractionError) as cx:
                extract_thumbnails(
                    input_path=Path("video.mp4"),
                    output_dir=self.tmp_path,
                    plans=[_plan("clip_001", 0.0, 10.0)],
                )
        # Message should point users at the right opt-out flag.
        self.assertIn("--export-thumbnails", str(cx.exception))

    def test_output_path_is_clip_id_jpg(self) -> None:
        plans = [_plan("clip_042", 0.0, 10.0)]
        with patch("src.thumbnail_extractor.subprocess.run") as run, \
             patch("src.thumbnail_extractor.shutil.which", return_value="/usr/bin/ffmpeg"):
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            results = extract_thumbnails(
                input_path=Path("video.mp4"),
                output_dir=self.tmp_path,
                plans=plans,
            )
        expected = self.tmp_path / "clip_042.jpg"
        self.assertEqual(results[0]["path"], str(expected))
        # Last positional arg of the ffmpeg cmd is the output path.
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[-1], str(expected))

    def test_output_dir_is_created(self) -> None:
        # Use a sub-path that doesn't exist yet — extract_thumbnails should
        # mkdir(parents=True) before invoking ffmpeg.
        target = self.tmp_path / "thumbnails" / "nested"
        self.assertFalse(target.exists())
        plans = [_plan("clip_001", 0.0, 10.0)]
        with patch("src.thumbnail_extractor.subprocess.run") as run, \
             patch("src.thumbnail_extractor.shutil.which", return_value="/usr/bin/ffmpeg"):
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            extract_thumbnails(
                input_path=Path("video.mp4"),
                output_dir=target,
                plans=plans,
            )
        self.assertTrue(target.is_dir())

    def test_empty_plans_list_returns_empty(self) -> None:
        with patch("src.thumbnail_extractor.subprocess.run") as run, \
             patch("src.thumbnail_extractor.shutil.which", return_value="/usr/bin/ffmpeg"):
            results = extract_thumbnails(
                input_path=Path("video.mp4"),
                output_dir=self.tmp_path,
                plans=[],
            )
        self.assertEqual(results, [])
        run.assert_not_called()

    def test_ffmpeg_command_uses_fast_seek_and_single_frame(self) -> None:
        # -ss BEFORE -i (fast keyframe seek), exactly one frame, -update 1
        # to avoid the "image2 needs %d" warning.
        plans = [_plan("clip_001", 30.0, 40.0)]
        with patch("src.thumbnail_extractor.subprocess.run") as run, \
             patch("src.thumbnail_extractor.shutil.which", return_value="/usr/bin/ffmpeg"):
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            extract_thumbnails(
                input_path=Path("video.mp4"),
                output_dir=self.tmp_path,
                plans=plans,
            )
        cmd = run.call_args.args[0]
        ss_idx = cmd.index("-ss")
        i_idx = cmd.index("-i")
        self.assertLess(ss_idx, i_idx, "-ss must come before -i for fast seek")
        self.assertIn("-frames:v", cmd)
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "1")
        self.assertIn("-update", cmd)


if __name__ == "__main__":
    unittest.main()
