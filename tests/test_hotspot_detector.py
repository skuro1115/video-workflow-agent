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
    CommentReactionDetector,
    CompositeDetector,
    EvenSamplingDetector,
    SubDetectorSpec,
    build_detector,
)
from src.score_weights import DetectorWeight, Weights


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

    def test_comment_reaction_requires_chat_log(self) -> None:
        with self.assertRaises(ValueError):
            build_detector("comment_reaction", count=3, window_seconds=20.0)

    def test_build_comment_reaction(self) -> None:
        det = build_detector(
            "comment_reaction", count=3, window_seconds=20.0,
            chat_log_path=Path("dummy.json"),
        )
        self.assertIsInstance(det, CommentReactionDetector)

    def test_composite_requires_weights(self) -> None:
        with self.assertRaises(ValueError):
            build_detector("composite", count=3, window_seconds=20.0)

    def test_build_composite_filters_unknown_subdetectors(self) -> None:
        weights = Weights(detectors=[
            DetectorWeight("audio_rms", 1.0),
            DetectorWeight("nonexistent", 1.0),
        ])
        det = build_detector(
            "composite", count=3, window_seconds=20.0, weights=weights,
        )
        self.assertIsInstance(det, CompositeDetector)
        self.assertEqual(len(det.sub_detectors), 1)
        self.assertEqual(det.sub_detectors[0].name, "audio_rms")


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


class CompositeDetectorTests(unittest.TestCase):
    """Composite uses canned sub-detectors (FakeDetector) so no ffmpeg/IO."""

    class _FakeDetector(EvenSamplingDetector):
        """Returns a fixed candidate list regardless of duration."""
        def __init__(self, fixed):
            self._fixed = fixed
        def detect(self, *, input_path, duration, debug_dir=None):
            return list(self._fixed)

    def _make(self, weights_and_cands):
        from src.hotspot_detector import HotspotCandidate
        sub = []
        for name, weight, cands in weights_and_cands:
            cand_objs = [HotspotCandidate(*c) for c in cands]
            sub.append(SubDetectorSpec(
                name=name,
                detector=self._FakeDetector(cand_objs),
                weight=weight,
            ))
        return sub

    def test_agreement_boosts_score(self) -> None:
        # Both detectors agree on a peak around t=60. The top pick must
        # overlap that region and credit both contributors.
        sub = self._make([
            ("d1", 1.0, [(50.0, 70.0, 1.0, "x"), (10.0, 30.0, 0.3, "y")]),
            ("d2", 1.0, [(55.0, 65.0, 1.0, "x"), (90.0, 100.0, 0.5, "y")]),
        ])
        det = CompositeDetector(
            sub, count=2, window_seconds=15.0, bin_seconds=1.0,
        )
        result = det.detect(input_path=Path("x"), duration=120.0)
        self.assertGreater(len(result), 0)
        top = max(result, key=lambda c: c.score)
        # Top pick window should overlap t=60.
        self.assertLessEqual(top.start, 60.0)
        self.assertGreaterEqual(top.end, 60.0)
        # And both detectors' names must appear in its reason.
        self.assertIn("d1", top.reason)
        self.assertIn("d2", top.reason)

    def test_zero_weight_excluded(self) -> None:
        # d2's region (0–10) must NOT show up as a pick, since its weight=0.
        # All picks must land within d1's region (50–70).
        sub = self._make([
            ("d1", 1.0, [(50.0, 70.0, 1.0, "x")]),
            ("d2", 0.0, [(0.0, 10.0, 1.0, "y")]),
        ])
        det = CompositeDetector(
            sub, count=3, window_seconds=15.0, bin_seconds=1.0,
        )
        result = det.detect(input_path=Path("x"), duration=120.0)
        self.assertGreater(len(result), 0)
        for c in result:
            mid = (c.start + c.end) / 2
            self.assertGreater(mid, 30.0,
                f"pick at {mid} leaked into the disabled-detector region")
            self.assertNotIn("d2", c.reason)

    def test_min_score_threshold(self) -> None:
        # The 0.3-score candidate region (t=10-30) must be excluded by the
        # high min_score; only the high-score region (t=50-70) survives.
        sub = self._make([
            ("d1", 1.0, [(50.0, 70.0, 1.0, "x"), (10.0, 30.0, 0.3, "y")]),
        ])
        det = CompositeDetector(
            sub, count=5, window_seconds=25.0, bin_seconds=1.0,
            min_score=0.95,
        )
        result = det.detect(input_path=Path("x"), duration=120.0)
        # window_seconds=25 > 20s peak → exactly one pick fits.
        self.assertEqual(len(result), 1)
        # And it lands in the high-score region.
        mid = (result[0].start + result[0].end) / 2
        self.assertGreater(mid, 40.0)
        self.assertLess(mid, 80.0)

    def test_subdetector_failure_is_skipped(self) -> None:
        from src.hotspot_detector import HotspotCandidate

        class Broken(EvenSamplingDetector):
            def __init__(self): pass
            def detect(self, **kw):
                raise RuntimeError("boom")

        good = self._FakeDetector([HotspotCandidate(50.0, 70.0, 1.0, "x")])
        sub = [
            SubDetectorSpec("broken", Broken(), 1.0),
            SubDetectorSpec("good", good, 1.0),
        ]
        det = CompositeDetector(
            sub, count=1, window_seconds=15.0, bin_seconds=1.0,
        )
        result = det.detect(input_path=Path("x"), duration=120.0)
        self.assertEqual(len(result), 1)
        self.assertIn("good", result[0].reason)

    def test_unknown_fusion_raises(self) -> None:
        with self.assertRaises(ValueError):
            CompositeDetector([], count=1, window_seconds=10.0, fusion="bogus")


class CompositeDetectorRrfTests(unittest.TestCase):
    """RRF fusion: ranks, not scores, drive the combined ordering."""

    class _FakeDetector(EvenSamplingDetector):
        def __init__(self, fixed):
            self._fixed = fixed
        def detect(self, *, input_path, duration, debug_dir=None):
            return list(self._fixed)

    def _make(self, weights_and_cands):
        from src.hotspot_detector import HotspotCandidate
        sub = []
        for name, weight, cands in weights_and_cands:
            cand_objs = [HotspotCandidate(*c) for c in cands]
            sub.append(SubDetectorSpec(
                name=name,
                detector=self._FakeDetector(cand_objs),
                weight=weight,
            ))
        return sub

    def test_outlier_score_does_not_dominate(self) -> None:
        # weighted_sum would let the outlier (score=100) at t=10 sweep, but
        # RRF only cares about rank, so an agreement region (t=60) where
        # both detectors rank a candidate first should win.
        sub = self._make([
            ("d1", 1.0, [
                (10.0, 30.0, 100.0, "outlier"),
                (50.0, 70.0, 1.0, "agree"),
            ]),
            ("d2", 1.0, [
                (50.0, 70.0, 1.0, "agree"),
                (90.0, 100.0, 0.5, "noise"),
            ]),
        ])
        det = CompositeDetector(
            sub, count=2, window_seconds=15.0, bin_seconds=1.0,
            fusion="rrf", rrf_k=60,
        )
        result = det.detect(input_path=Path("x"), duration=120.0)
        self.assertGreater(len(result), 0)
        top = max(result, key=lambda c: c.score)
        # Top pick should be in the agreement zone (50-70), not the outlier (10-30).
        center = (top.start + top.end) / 2
        self.assertGreater(center, 40.0)
        self.assertLess(center, 80.0)
        self.assertIn("rrf", top.reason)
        self.assertIn("rank=", top.reason)

    def test_reason_shows_ranks(self) -> None:
        sub = self._make([
            ("d1", 1.0, [(50.0, 70.0, 1.0, "x"), (10.0, 30.0, 0.5, "y")]),
            ("d2", 1.0, [(50.0, 70.0, 1.0, "x")]),
        ])
        det = CompositeDetector(
            sub, count=1, window_seconds=15.0, bin_seconds=1.0, fusion="rrf",
        )
        result = det.detect(input_path=Path("x"), duration=120.0)
        self.assertEqual(len(result), 1)
        # Best detectors rank this region #1 → reason should say rank=1
        self.assertIn("rank=1", result[0].reason)

    def test_zero_weight_excluded_in_rrf(self) -> None:
        sub = self._make([
            ("d1", 1.0, [(50.0, 70.0, 1.0, "x")]),
            ("d2", 0.0, [(0.0, 10.0, 1.0, "y")]),
        ])
        det = CompositeDetector(
            sub, count=3, window_seconds=15.0, bin_seconds=1.0, fusion="rrf",
        )
        result = det.detect(input_path=Path("x"), duration=120.0)
        for c in result:
            self.assertNotIn("d2", c.reason)
            mid = (c.start + c.end) / 2
            self.assertGreater(mid, 30.0)

    def test_score_normalised_to_unit_interval(self) -> None:
        # All three detectors agree on a single peak, ranked #1 from each.
        # Combined score should be ~1.0 (max possible).
        sub = self._make([
            (f"d{i}", 1.0, [(50.0, 60.0, 1.0, "agree")])
            for i in range(3)
        ])
        det = CompositeDetector(
            sub, count=1, window_seconds=10.0, bin_seconds=1.0, fusion="rrf",
        )
        result = det.detect(input_path=Path("x"), duration=120.0)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0].score, 1.0, places=2)


class CommentReactionDetectorTests(unittest.TestCase):
    """Reaction-token weighting on top of the chat-log signal."""

    def _write_chat(self, messages: list[dict]) -> Path:
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8",
        )
        json.dump(messages, f, ensure_ascii=False)
        f.close()
        return Path(f.name)

    def test_reactions_outscore_plain_density(self) -> None:
        # Cluster A (t≈15): 8 unique users, all greetings (no reactions).
        # Cluster B (t≈60): 4 unique users, all reactions.
        # CommentDensityDetector would prefer A (more users); reaction
        # detector must prefer B (only B has reactive content).
        msgs = (
            [{"t": 15.0 + 0.1 * i, "user": f"u{i}", "text": "hi"} for i in range(8)] +
            [
                {"t": 60.0, "user": "a", "text": "草"},
                {"t": 60.5, "user": "b", "text": "wwww"},
                {"t": 61.0, "user": "c", "text": "lol"},
                {"t": 61.5, "user": "d", "text": "🤣"},
            ]
        )
        path = self._write_chat(msgs)
        try:
            det = CommentReactionDetector(
                count=2, window_seconds=20.0, chat_log_path=path,
            )
            result = det.detect(input_path=Path("dummy"), duration=120.0)
        finally:
            path.unlink()

        self.assertGreater(len(result), 0)
        top = max(result, key=lambda c: c.score)
        center = (top.start + top.end) / 2
        self.assertGreater(center, 50.0,
            "reaction-only cluster (t≈60) should outrank greeting cluster (t≈15)")
        self.assertLess(center, 70.0)
        self.assertIn("audience reaction", top.reason)

    def test_spammer_does_not_dominate(self) -> None:
        # 1 spammer firing 50 reactive messages vs 5 unique users with 1 each.
        # Per-user-best logic should make the 5-user burst outrank the spammer
        # by a factor of ~5.
        spam = [{"t": 10.0 + 0.05 * i, "user": "spam", "text": "草"} for i in range(50)]
        real = [{"t": 60.0 + i * 0.5, "user": f"u{i}", "text": "lol"} for i in range(5)]
        path = self._write_chat(spam + real)
        try:
            det = CommentReactionDetector(
                count=2, window_seconds=15.0, chat_log_path=path,
            )
            result = det.detect(input_path=Path("dummy"), duration=120.0)
        finally:
            path.unlink()

        self.assertEqual(len(result), 2)
        sorted_by_score = sorted(result, key=lambda c: c.score, reverse=True)
        top_center = (sorted_by_score[0].start + sorted_by_score[0].end) / 2
        self.assertGreater(top_center, 50.0,
            "5-user burst should beat 1-spammer flood")
        self.assertLess(top_center, 70.0)

    def test_w_run_matches_both_widths(self) -> None:
        msgs = [
            {"t": 30.0, "user": "a", "text": "ｗｗｗｗ"},   # fullwidth
            {"t": 30.5, "user": "b", "text": "wwww"},      # halfwidth
            {"t": 31.0, "user": "c", "text": "ww"},        # short run still counts
            # And a control bin with non-reactive plain text:
            {"t": 80.0, "user": "x", "text": "test"},
            {"t": 80.5, "user": "y", "text": "noreact"},
        ]
        path = self._write_chat(msgs)
        try:
            det = CommentReactionDetector(
                count=2, window_seconds=10.0, chat_log_path=path,
            )
            result = det.detect(input_path=Path("dummy"), duration=120.0)
        finally:
            path.unlink()

        # Only the w-run cluster (t=30) is reactive; control bin (t=80) is silent.
        self.assertEqual(len(result), 1)
        center = (result[0].start + result[0].end) / 2
        self.assertGreater(center, 25.0)
        self.assertLess(center, 40.0)
        self.assertIn("w連投", result[0].reason)

    def test_word_boundary_avoids_false_positive(self) -> None:
        # "lol" must NOT match inside "lolly" or "blowfish".
        # If boundary matching is broken, this bin would be picked.
        msgs = [
            {"t": 10.0, "user": "a", "text": "lolly"},
            {"t": 10.5, "user": "b", "text": "blowfish"},
            {"t": 11.0, "user": "c", "text": "wowza"},  # "wow" inside another word
        ]
        path = self._write_chat(msgs)
        try:
            det = CommentReactionDetector(
                count=1, window_seconds=10.0, chat_log_path=path,
            )
            result = det.detect(input_path=Path("dummy"), duration=60.0)
        finally:
            path.unlink()
        self.assertEqual(result, [],
            "ASCII tokens must use word-boundary matching, not raw substring")

    def test_per_message_strength_capped(self) -> None:
        # A single message stuffed with many reactions should not dominate
        # an entire bin — its strength is capped at MAX_PER_MESSAGE.
        msgs = [
            {"t": 10.0, "user": "stuffer", "text": "草 lol wow omg やばい 🤣 wwww すごい"},
            # Compare against a bin where 5 unique users each typed one reaction.
            {"t": 60.0, "user": "a", "text": "草"},
            {"t": 60.5, "user": "b", "text": "lol"},
            {"t": 61.0, "user": "c", "text": "wow"},
            {"t": 61.5, "user": "d", "text": "草"},
            {"t": 62.0, "user": "e", "text": "lol"},
        ]
        path = self._write_chat(msgs)
        try:
            det = CommentReactionDetector(
                count=2, window_seconds=15.0, chat_log_path=path,
            )
            result = det.detect(input_path=Path("dummy"), duration=120.0)
        finally:
            path.unlink()

        sorted_by_score = sorted(result, key=lambda c: c.score, reverse=True)
        top_center = (sorted_by_score[0].start + sorted_by_score[0].end) / 2
        # Bin score = sum of per-user max strengths (capped at MAX_PER_MESSAGE=3).
        # Stuffer bin: 1 user × min(8,3) = 3.
        # 5-user bin: 5 × 1 = 5. Should win.
        self.assertGreater(top_center, 50.0,
            "5-user single-reaction bin should beat 1-user reaction-stuffed bin")

    def test_no_reactions_returns_empty(self) -> None:
        msgs = [
            {"t": 10.0, "user": "a", "text": "good morning"},
            {"t": 20.0, "user": "b", "text": "hello there"},
        ]
        path = self._write_chat(msgs)
        try:
            det = CommentReactionDetector(
                count=3, window_seconds=10.0, chat_log_path=path,
            )
            self.assertEqual(
                det.detect(input_path=Path("dummy"), duration=60.0),
                [],
            )
        finally:
            path.unlink()

    def test_missing_file_raises(self) -> None:
        det = CommentReactionDetector(
            count=3, window_seconds=10.0,
            chat_log_path=Path("/nonexistent/chat.json"),
        )
        with self.assertRaises(FileNotFoundError):
            det.detect(input_path=Path("dummy"), duration=60.0)

    def test_reason_lists_top_tokens(self) -> None:
        # 草 should win the count race.
        msgs = (
            [{"t": 10.0 + 0.1 * i, "user": f"u{i}", "text": "草"} for i in range(5)] +
            [{"t": 10.5 + 0.1 * i, "user": f"v{i}", "text": "lol"} for i in range(2)]
        )
        path = self._write_chat(msgs)
        try:
            det = CommentReactionDetector(
                count=1, window_seconds=10.0, chat_log_path=path,
            )
            result = det.detect(input_path=Path("dummy"), duration=60.0)
        finally:
            path.unlink()
        self.assertEqual(len(result), 1)
        # Reason should put 草 first (it has higher count).
        reason = result[0].reason
        self.assertIn("草", reason)
        self.assertIn("lol", reason)
        kusa_pos = reason.find("草")
        lol_pos = reason.find("lol")
        self.assertLess(kusa_pos, lol_pos,
            "reason should list tokens by count descending (草 first)")

    def test_custom_token_lists(self) -> None:
        # Override defaults with a custom list — only "AYAYA" should match.
        msgs = [
            {"t": 10.0, "user": "a", "text": "草"},      # default would match, but disabled
            {"t": 10.5, "user": "b", "text": "wwww"},   # default w-run still matches
            {"t": 30.0, "user": "c", "text": "AYAYA"},  # custom token
        ]
        path = self._write_chat(msgs)
        try:
            det = CommentReactionDetector(
                count=3, window_seconds=10.0, chat_log_path=path,
                boundary_tokens=("AYAYA",),
                plain_tokens=(),
            )
            result = det.detect(input_path=Path("dummy"), duration=60.0)
        finally:
            path.unlink()
        # Two bins reactive: t=10 (wwww via w-run) and t=30 (AYAYA via custom).
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
