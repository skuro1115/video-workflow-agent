"""Tests for the RunTimer used to record per-stage pipeline timings."""
from __future__ import annotations

import time
import unittest

from src.run_timer import RunTimer


class RunTimerTests(unittest.TestCase):
    def test_records_stages_in_order(self) -> None:
        t = RunTimer()
        with t.stage("a"):
            pass
        with t.stage("b"):
            pass
        names = [s.name for s in t.stages]
        self.assertEqual(names, ["a", "b"])

    def test_elapsed_is_nonnegative_and_roughly_correct(self) -> None:
        t = RunTimer()
        with t.stage("sleep_a_bit"):
            time.sleep(0.01)
        s = t.stages[0]
        self.assertGreaterEqual(s.elapsed_seconds, 0.005)
        self.assertLess(s.elapsed_seconds, 1.0)

    def test_extra_kwargs_propagate_to_dict(self) -> None:
        t = RunTimer()
        with t.stage("detect", detector="audio_rms"):
            pass
        d = t.to_dict()
        self.assertEqual(d["stages"][0]["name"], "detect")
        self.assertEqual(d["stages"][0]["detector"], "audio_rms")

    def test_stage_records_even_on_exception(self) -> None:
        t = RunTimer()
        try:
            with t.stage("crash"):
                raise RuntimeError("nope")
        except RuntimeError:
            pass
        self.assertEqual(len(t.stages), 1)
        self.assertEqual(t.stages[0].name, "crash")

    def test_total_seconds_is_sum_of_stages(self) -> None:
        t = RunTimer()
        with t.stage("a"):
            time.sleep(0.005)
        with t.stage("b"):
            time.sleep(0.005)
        d = t.to_dict()
        # total is computed from raw, then rounded; per-stage values are
        # rounded then summed. The rounding can disagree by ~1e-4.
        self.assertAlmostEqual(
            d["total_seconds"],
            sum(s["elapsed_seconds"] for s in d["stages"]),
            delta=2e-4,
        )

    def test_to_dict_empty(self) -> None:
        d = RunTimer().to_dict()
        self.assertEqual(d, {"stages": [], "total_seconds": 0})


if __name__ == "__main__":
    unittest.main()
