"""Pure-logic tests for clip_planner.

Run with: ``python -m unittest discover -s tests``
"""
from __future__ import annotations

import unittest

from src.clip_planner import plan_clips
from src.hotspot_detector import HotspotCandidate


def _candidate(start: float, end: float, score: float = 0.5, reason: str = "x") -> HotspotCandidate:
    return HotspotCandidate(start=start, end=end, score=score, reason=reason)


class PlanClipsTests(unittest.TestCase):
    def test_empty_input_returns_empty(self) -> None:
        self.assertEqual(plan_clips([], min_duration=10.0, max_duration=60.0), [])

    def test_drops_too_short(self) -> None:
        cands = [_candidate(0.0, 5.0), _candidate(20.0, 35.0)]
        plans = plan_clips(cands, min_duration=10.0, max_duration=60.0)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].clip_id, "clip_001")
        self.assertEqual(plans[0].source_start, 20.0)

    def test_truncates_too_long(self) -> None:
        cands = [_candidate(0.0, 90.0)]
        plans = plan_clips(cands, min_duration=10.0, max_duration=60.0)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].duration, 60.0)
        self.assertEqual(plans[0].source_start, 0.0)
        self.assertEqual(plans[0].source_end, 60.0)

    def test_clip_id_sequencing(self) -> None:
        cands = [_candidate(0.0, 30.0), _candidate(60.0, 90.0), _candidate(120.0, 150.0)]
        plans = plan_clips(cands, min_duration=10.0, max_duration=60.0)
        self.assertEqual([p.clip_id for p in plans], ["clip_001", "clip_002", "clip_003"])

    def test_score_and_reason_propagate(self) -> None:
        cands = [_candidate(0.0, 30.0, score=0.9, reason="loud")]
        plans = plan_clips(cands, min_duration=10.0, max_duration=60.0)
        self.assertEqual(plans[0].score, 0.9)
        self.assertEqual(plans[0].reason, "loud")
        self.assertEqual(plans[0].purpose, "short clip candidate")
        self.assertEqual(plans[0].status, "planned")

    def test_dropped_candidate_does_not_consume_id(self) -> None:
        # First (too short) should be dropped; surviving ones get 001, 002.
        cands = [_candidate(0.0, 3.0), _candidate(10.0, 40.0), _candidate(60.0, 90.0)]
        plans = plan_clips(cands, min_duration=10.0, max_duration=60.0)
        self.assertEqual([p.clip_id for p in plans], ["clip_001", "clip_002"])


if __name__ == "__main__":
    unittest.main()
