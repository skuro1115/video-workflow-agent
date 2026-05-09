"""Pure-logic tests for the hotspot detectors.

Run with: ``python -m unittest discover -s tests``

These tests do NOT touch ffmpeg. The audio_rms detector is exercised by
patching its private ``_extract_rms_series`` method to return a synthetic
RMS series, so the test suite has zero external dependencies.
"""
from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.hotspot_detector import (
    AudioRmsDetector,
    CommentDensityDetector,
    EvenSamplingDetector,
    build_detector,
)


class EvenSamplingDetectorTests(unittest.TestCase):
    def test_zero_duration_returns_empty(self) -> None:
        det = EvenSamplingDetector(count=4, window_seconds=20.0)
        self.assertEqual(det.detect(input_path=Path("x"), duration=0.0), [])

    def test_negative_duration_returns_empty(self) -> None:
        det = EvenSamplingDetector(count=4, window_seconds=20.0)
        self.assertEqual(det.detect(input_path=Path("x"), duration=-1.0), [])

    def test_duration_shorter_than_window_returns_single(self) -> None:
        det = EvenSamplingDetector(count=6, window_seconds=20.0)
        result = det.detect(input_path=Path("x"), duration=10.0)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].start, 0.0)
        self.assertEqual(result[0].end, 10.0)

    def test_count_one_returns_single(self) -> None:
        det = EvenSamplingDetector(count=1, window_seconds=20.0)
        result = det.detect(input_path=Path("x"), duration=120.0)
        self.assertEqual(len(result), 1)

    def test_evenly_spaced(self) -> None:
        det = EvenSamplingDetector(count=4, window_seconds=20.0)
        result = det.detect(input_path=Path("x"), duration=120.0)
        self.assertEqual(len(result), 4)
        # First starts at 0, last ends at duration.
        self.assertEqual(result[0].start, 0.0)
        self.assertAlmostEqual(result[-1].end, 120.0, places=2)
        # Spacing is uniform: (120 - 20) / 3 = 33.333
        self.assertAlmostEqual(result[1].start - result[0].start, 33.333, places=2)
        self.assertAlmostEqual(result[2].start - result[1].start, 33.333, places=2)

    def test_score_and_reason_are_set(self) -> None:
        det = EvenSamplingDetector(count=2, window_seconds=10.0)
        result = det.detect(input_path=Path("x"), duration=60.0)
        for cand in result:
            self.assertEqual(cand.score, 0.5)
            self.assertIn("placeholder", cand.reason)

    def test_window_too_small_clamped_to_one(self) -> None:
        det = EvenSamplingDetector(count=2, window_seconds=0.1)
        # constructor clamps window to >= 1.0
        self.assertEqual(det.window_seconds, 1.0)


class AudioRmsDetectorTests(unittest.TestCase):
    """Tests that mock the ffmpeg/PCM extraction step."""

    def _series(self, points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return list(points)

    def test_picks_top_k_by_loudness(self) -> None:
        det = AudioRmsDetector(count=3, window_seconds=10.0)
        # Loudness peaks at t=20, 60, 100. Quiet noise everywhere else.
        series = self._series([
            (0.0, -40.0), (10.0, -38.0), (20.0, -10.0),  # peak
            (30.0, -39.0), (40.0, -41.0),
            (50.0, -38.0), (60.0, -8.0),                  # peak (loudest)
            (70.0, -40.0), (80.0, -42.0), (90.0, -37.0),
            (100.0, -12.0),                               # peak
            (110.0, -41.0),
        ])
        with patch.object(AudioRmsDetector, "_extract_rms_series", return_value=series):
            result = det.detect(input_path=Path("x"), duration=120.0)
        self.assertEqual(len(result), 3)
        starts = [c.start for c in result]
        # Sorted ascending in output.
        self.assertEqual(starts, sorted(starts))
        # The three peaks should be roughly centered at 20/60/100.
        peak_ts = sorted(c.start + c.end for c in result)  # midpoint*2 proxy
        # Loudest (t=60, -8 dBFS) should map to score=1.0 after min-max norm.
        scores = sorted([c.score for c in result], reverse=True)
        self.assertAlmostEqual(scores[0], 1.0, places=2)
        # All scores in [0, 1]
        for c in result:
            self.assertGreaterEqual(c.score, 0.0)
            self.assertLessEqual(c.score, 1.0)
            self.assertIn("dBFS", c.reason)

    def test_nms_prevents_overlapping_picks(self) -> None:
        det = AudioRmsDetector(count=3, window_seconds=20.0)
        # The two loudest bins (t=10, t=15) are within the NMS gap, so only
        # one of them can be picked. NMS contract: no two picked bin-centers
        # are within window_seconds of each other.
        series = self._series([
            (0.0, -40.0), (5.0, -38.0),
            (10.0, -5.0),   # loudest
            (15.0, -6.0),   # second-loudest, within 20s of t=10 → must skip
            (20.0, -39.0),
            (60.0, -10.0),  # far enough → picked
            (90.0, -25.0),  # far enough → picked
        ])
        with patch.object(AudioRmsDetector, "_extract_rms_series", return_value=series):
            result = det.detect(input_path=Path("x"), duration=100.0)
        # Verify NMS contract directly: every pair of pick centers ≥ window_seconds apart.
        centers = [(c.start + c.end) / 2 for c in result]
        for i, ci in enumerate(centers):
            for cj in centers[i + 1 :]:
                self.assertGreaterEqual(
                    abs(ci - cj), 20.0,
                    f"NMS violated: picks at {ci:.1f} and {cj:.1f} are <20s apart",
                )
        # And the suppressed t=15 bin must NOT show up as a pick.
        for c in result:
            self.assertFalse(
                12.5 <= (c.start + c.end) / 2 <= 17.5,
                "t=15 bin should have been suppressed by NMS",
            )

    def test_empty_audio_returns_empty(self) -> None:
        det = AudioRmsDetector(count=3, window_seconds=10.0)
        with patch.object(AudioRmsDetector, "_extract_rms_series", return_value=[]):
            self.assertEqual(det.detect(input_path=Path("x"), duration=60.0), [])

    def test_all_silence_returns_empty(self) -> None:
        det = AudioRmsDetector(count=3, window_seconds=10.0)
        # All -inf values.
        series = [(float(i), float("-inf")) for i in range(10)]
        with patch.object(AudioRmsDetector, "_extract_rms_series", return_value=series):
            self.assertEqual(det.detect(input_path=Path("x"), duration=60.0), [])

    def test_window_clamped_to_audio_span(self) -> None:
        det = AudioRmsDetector(count=1, window_seconds=30.0)
        # Peak right at the start; window can't extend past 0.
        series = [(0.0, -5.0), (1.0, -40.0), (2.0, -42.0)]
        with patch.object(AudioRmsDetector, "_extract_rms_series", return_value=series):
            result = det.detect(input_path=Path("x"), duration=60.0)
        self.assertEqual(len(result), 1)
        self.assertGreaterEqual(result[0].start, 0.0)
        self.assertLessEqual(result[0].end, 30.0 + 1.0)  # bin_seconds slack


class FactoryTests(unittest.TestCase):
    def test_build_even(self) -> None:
        det = build_detector("even", count=3, window_seconds=20.0)
        self.assertIsInstance(det, EvenSamplingDetector)

    def test_build_audio_rms(self) -> None:
        det = build_detector("audio_rms", count=3, window_seconds=20.0)
        self.assertIsInstance(det, AudioRmsDetector)

    def test_unknown_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_detector("nonexistent", count=3, window_seconds=20.0)

    def test_comment_density_requires_chat_log(self) -> None:
        with self.assertRaises(ValueError):
            build_detector("comment_density", count=3, window_seconds=20.0)


class CommentDensityDetectorTests(unittest.TestCase):
    def _write_chat(self, messages: list[dict]) -> Path:
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8",
        )
        json.dump(messages, f, ensure_ascii=False)
        f.close()
        return Path(f.name)

    def test_picks_dense_window(self) -> None:
        # Three burst clusters around t=15, t=60, t=95.
        msgs = (
            [{"t": 15.0 + 0.1 * i, "user": f"u{i}", "text": "x"} for i in range(8)] +
            [{"t": 60.0 + 0.1 * i, "user": f"v{i}", "text": "x"} for i in range(12)] +
            [{"t": 95.0 + 0.1 * i, "user": f"w{i}", "text": "x"} for i in range(6)]
        )
        path = self._write_chat(msgs)
        try:
            det = CommentDensityDetector(
                count=3, window_seconds=20.0, chat_log_path=path,
            )
            result = det.detect(input_path=Path("dummy"), duration=120.0)
        finally:
            path.unlink()

        self.assertEqual(len(result), 3)
        # Loudest cluster (t≈60, 12 unique users) should have score=1.0.
        scores_sorted = sorted([c.score for c in result], reverse=True)
        self.assertAlmostEqual(scores_sorted[0], 1.0, places=2)
        for c in result:
            self.assertIn("comment density", c.reason)

    def test_unique_users_not_message_count(self) -> None:
        # 1 spammer with 50 messages vs 5 unique users with 1 message each.
        msgs_spam = [{"t": 10.0 + 0.05 * i, "user": "spammer", "text": "."} for i in range(50)]
        msgs_real = [{"t": 60.0 + i, "user": f"u{i}", "text": "wow"} for i in range(5)]
        path = self._write_chat(msgs_spam + msgs_real)
        try:
            det = CommentDensityDetector(
                count=2, window_seconds=15.0, chat_log_path=path,
            )
            result = det.detect(input_path=Path("dummy"), duration=120.0)
        finally:
            path.unlink()

        # The 5-unique-user burst should outrank the 1-spammer flood.
        self.assertEqual(len(result), 2)
        sorted_by_score = sorted(result, key=lambda c: c.score, reverse=True)
        # Top pick centered around t=60 (the real users).
        top_center = (sorted_by_score[0].start + sorted_by_score[0].end) / 2
        self.assertGreater(top_center, 50.0)
        self.assertLess(top_center, 70.0)

    def test_missing_file_raises(self) -> None:
        det = CommentDensityDetector(
            count=3, window_seconds=10.0,
            chat_log_path=Path("/nonexistent/chat.json"),
        )
        with self.assertRaises(FileNotFoundError):
            det.detect(input_path=Path("dummy"), duration=60.0)

    def test_empty_chat_returns_empty(self) -> None:
        path = self._write_chat([])
        try:
            det = CommentDensityDetector(
                count=3, window_seconds=10.0, chat_log_path=path,
            )
            self.assertEqual(
                det.detect(input_path=Path("dummy"), duration=60.0),
                [],
            )
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main()
