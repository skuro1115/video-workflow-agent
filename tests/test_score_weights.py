"""Tests for the score_weights module.

Covers JSON load/save round-trip, malformed config detection, and the
interactive prompt flow with mocked stdin/stdout.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from src.score_weights import (
    DetectorWeight,
    Weights,
    WeightsConfigError,
    default_weights,
    interactive_weights,
    load_weights,
    save_weights,
)


class WeightsRoundTripTests(unittest.TestCase):
    def test_save_and_load(self) -> None:
        w = Weights(
            detectors=[
                DetectorWeight(name="audio_rms", weight=1.5),
                DetectorWeight(name="comment_density", weight=2.0),
            ],
            bin_seconds=2.0,
            min_score=0.1,
        )
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "weights.json"
            save_weights(p, w)
            loaded = load_weights(p)
        self.assertEqual(len(loaded.detectors), 2)
        self.assertEqual(loaded.detectors[0].name, "audio_rms")
        self.assertEqual(loaded.detectors[0].weight, 1.5)
        self.assertEqual(loaded.bin_seconds, 2.0)
        self.assertEqual(loaded.min_score, 0.1)

    def test_default_weights_are_loadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "w.json"
            save_weights(p, default_weights())
            loaded = load_weights(p)
        self.assertGreater(len(loaded.detectors), 0)


class WeightsValidationTests(unittest.TestCase):
    def test_missing_file_raises(self) -> None:
        with self.assertRaises(WeightsConfigError):
            load_weights(Path("/nonexistent/weights.json"))

    def test_invalid_json_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text("{not valid json")
            with self.assertRaises(WeightsConfigError):
                load_weights(p)

    def test_top_level_must_be_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text("[1, 2, 3]")
            with self.assertRaises(WeightsConfigError):
                load_weights(p)

    def test_detector_entry_missing_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text(json.dumps({"detectors": [{"weight": 1.0}]}))
            with self.assertRaises(WeightsConfigError):
                load_weights(p)


class WeightsAccessorTests(unittest.TestCase):
    def test_enabled_filters_zero_weights(self) -> None:
        w = Weights(detectors=[
            DetectorWeight("a", 1.0),
            DetectorWeight("b", 0.0),
            DetectorWeight("c", 0.5),
        ])
        names = [d.name for d in w.enabled()]
        self.assertEqual(names, ["a", "c"])


class InteractivePromptTests(unittest.TestCase):
    def test_blank_input_keeps_default(self) -> None:
        in_stream = io.StringIO("\n\n\n\n")  # blank for each prompt
        out_stream = io.StringIO()
        defaults = Weights(
            detectors=[DetectorWeight("audio_rms", 1.5), DetectorWeight("comment_density", 2.0)],
            bin_seconds=1.0,
            min_score=0.0,
        )
        result = interactive_weights(
            ["audio_rms", "comment_density"],
            defaults=defaults,
            in_stream=in_stream,
            out_stream=out_stream,
        )
        self.assertEqual(result.detectors[0].weight, 1.5)
        self.assertEqual(result.detectors[1].weight, 2.0)
        self.assertEqual(result.bin_seconds, 1.0)
        self.assertEqual(result.min_score, 0.0)

    def test_user_overrides_weights(self) -> None:
        in_stream = io.StringIO("3.0\n0.5\n2.0\n0.1\n")
        out_stream = io.StringIO()
        defaults = Weights(
            detectors=[DetectorWeight("audio_rms", 1.0), DetectorWeight("comment_density", 1.0)],
        )
        result = interactive_weights(
            ["audio_rms", "comment_density"],
            defaults=defaults,
            in_stream=in_stream,
            out_stream=out_stream,
        )
        self.assertEqual(result.detectors[0].weight, 3.0)
        self.assertEqual(result.detectors[1].weight, 0.5)
        self.assertEqual(result.bin_seconds, 2.0)
        self.assertEqual(result.min_score, 0.1)

    def test_invalid_number_falls_back_to_default(self) -> None:
        in_stream = io.StringIO("not-a-number\n\n\n\n")
        out_stream = io.StringIO()
        defaults = Weights(detectors=[DetectorWeight("audio_rms", 1.0)])
        result = interactive_weights(
            ["audio_rms"], defaults=defaults,
            in_stream=in_stream, out_stream=out_stream,
        )
        self.assertEqual(result.detectors[0].weight, 1.0)
        self.assertIn("could not parse", out_stream.getvalue())


if __name__ == "__main__":
    unittest.main()
