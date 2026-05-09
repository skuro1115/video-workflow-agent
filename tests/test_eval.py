"""Tests for scripts/eval.py.

Pure logic — no filesystem touches except the file-load tests, which use
tempfile. Run with: ``python -m unittest discover -s tests``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.eval import (
    Candidate,
    Peak,
    evaluate,
    format_summary,
    load_candidates,
    load_peaks,
)


class PeakFromDictTests(unittest.TestCase):
    def test_t_range(self) -> None:
        p = Peak.from_dict({"t_range": [10.0, 20.0], "label": "x"})
        self.assertEqual((p.start, p.end, p.label), (10.0, 20.0, "x"))

    def test_single_t_default_tolerance(self) -> None:
        p = Peak.from_dict({"t": 60.0, "label": "y"})
        # default tolerance is 5s, so 55..65
        self.assertEqual((p.start, p.end), (55.0, 65.0))

    def test_single_t_custom_tolerance(self) -> None:
        p = Peak.from_dict({"t": 60.0, "tolerance": 2.5, "label": "z"})
        self.assertEqual((p.start, p.end), (57.5, 62.5))

    def test_missing_keys_raises(self) -> None:
        with self.assertRaises(ValueError):
            Peak.from_dict({"label": "no time"})

    def test_inverted_range_raises(self) -> None:
        with self.assertRaises(ValueError):
            Peak.from_dict({"t_range": [20.0, 10.0], "label": "bad"})

    def test_t_range_wrong_length_raises(self) -> None:
        with self.assertRaises(ValueError):
            Peak.from_dict({"t_range": [10.0], "label": "bad"})


class EvaluateTests(unittest.TestCase):
    def test_perfect_hit(self) -> None:
        peaks = [Peak(15.0, 20.0, "p1"), Peak(55.0, 65.0, "p2")]
        cands = [
            Candidate(start=14.0, end=21.0, score=1.0, reason=""),
            Candidate(start=54.0, end=66.0, score=0.8, reason=""),
        ]
        r = evaluate(peaks, cands)
        self.assertEqual(r.hit_peaks, 2)
        self.assertEqual(r.hit_rate, 1.0)
        self.assertEqual(r.precision, 1.0)
        self.assertEqual(r.misses, [])

    def test_partial_overlap_counts_as_hit(self) -> None:
        # candidate just barely touches the peak range
        peaks = [Peak(15.0, 20.0, "p1")]
        cands = [Candidate(start=19.5, end=25.0, score=0.5, reason="")]
        r = evaluate(peaks, cands)
        self.assertEqual(r.hit_peaks, 1)

    def test_no_overlap_is_miss(self) -> None:
        peaks = [Peak(15.0, 20.0, "p1")]
        cands = [Candidate(start=30.0, end=40.0, score=0.5, reason="")]
        r = evaluate(peaks, cands)
        self.assertEqual(r.hit_peaks, 0)
        self.assertEqual(r.hit_rate, 0.0)
        self.assertEqual(len(r.misses), 1)
        self.assertEqual(r.misses[0]["label"], "p1")

    def test_one_candidate_can_satisfy_multiple_peaks(self) -> None:
        peaks = [Peak(15.0, 18.0, "p1"), Peak(20.0, 22.0, "p2")]
        cands = [Candidate(start=10.0, end=25.0, score=1.0, reason="")]
        r = evaluate(peaks, cands)
        self.assertEqual(r.hit_peaks, 2)
        self.assertEqual(r.candidates_overlapping, 1)
        self.assertEqual(r.precision, 1.0)

    def test_extra_candidates_lower_precision(self) -> None:
        # 1 hit-overlap candidate + 2 unrelated = precision 1/3
        peaks = [Peak(15.0, 20.0, "p1")]
        cands = [
            Candidate(start=15.0, end=20.0, score=1.0, reason=""),
            Candidate(start=40.0, end=50.0, score=0.7, reason=""),
            Candidate(start=80.0, end=90.0, score=0.5, reason=""),
        ]
        r = evaluate(peaks, cands)
        self.assertEqual(r.hit_peaks, 1)
        self.assertEqual(r.candidates_overlapping, 1)
        self.assertAlmostEqual(r.precision, 1 / 3, places=4)

    def test_empty_inputs(self) -> None:
        self.assertEqual(evaluate([], []).hit_rate, 0.0)
        self.assertEqual(evaluate([], []).precision, 0.0)

    def test_no_candidates_all_miss(self) -> None:
        peaks = [Peak(0.0, 1.0, "p1"), Peak(2.0, 3.0, "p2")]
        r = evaluate(peaks, [])
        self.assertEqual(r.hit_peaks, 0)
        self.assertEqual(len(r.misses), 2)


class LoadersTests(unittest.TestCase):
    def _write(self, payload, suffix: str = ".json") -> Path:
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, delete=False, encoding="utf-8",
        )
        json.dump(payload, f, ensure_ascii=False)
        f.close()
        return Path(f.name)

    def test_load_peaks_object_form(self) -> None:
        path = self._write({
            "video": "x.mp4",
            "duration": 120.0,
            "peaks": [
                {"t_range": [15.0, 20.0], "label": "a"},
                {"t": 60.0, "label": "b"},
            ],
        })
        try:
            peaks = load_peaks(path)
            self.assertEqual(len(peaks), 2)
            self.assertEqual(peaks[0].label, "a")
        finally:
            path.unlink()

    def test_load_peaks_bare_list(self) -> None:
        path = self._write([{"t_range": [0.0, 1.0], "label": "x"}])
        try:
            peaks = load_peaks(path)
            self.assertEqual(len(peaks), 1)
        finally:
            path.unlink()

    def test_load_candidates(self) -> None:
        path = self._write([
            {"start": 1.0, "end": 5.0, "score": 0.8, "reason": "x"},
            {"start": 10.0, "end": 15.0, "score": 0.5, "reason": "y"},
        ])
        try:
            cands = load_candidates(path)
            self.assertEqual(len(cands), 2)
            self.assertEqual(cands[0].score, 0.8)
        finally:
            path.unlink()


class FormatSummaryTests(unittest.TestCase):
    def test_includes_misses(self) -> None:
        peaks = [Peak(15.0, 20.0, "burst1"), Peak(50.0, 55.0, "burst2")]
        cands = [Candidate(start=15.0, end=20.0, score=1.0, reason="")]
        text = format_summary(evaluate(peaks, cands))
        self.assertIn("1/2 hit", text)
        self.assertIn("burst2", text)


if __name__ == "__main__":
    unittest.main()
