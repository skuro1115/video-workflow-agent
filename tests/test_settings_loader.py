"""Tests for the unified settings JSON loader."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.settings_loader import SettingsLoadError, load_settings


def _write(payload) -> Path:
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8",
    )
    json.dump(payload, f, ensure_ascii=False)
    f.close()
    return Path(f.name)


class SettingsLoaderTests(unittest.TestCase):
    def test_minimal_file_loads_empty_defaults(self) -> None:
        p = _write({"_comment": "nothing functional here"})
        try:
            defaults, weights = load_settings(p)
        finally:
            p.unlink()
        self.assertEqual(defaults, {})
        self.assertIsNone(weights)

    def test_paths_are_converted(self) -> None:
        p = _write({
            "input": "videos/x.mp4",
            "output": "out/",
            "chat_log": "chat.json",
            "weights_path": "w.json",
        })
        try:
            defaults, _ = load_settings(p)
        finally:
            p.unlink()
        self.assertEqual(defaults["input"], Path("videos/x.mp4"))
        self.assertEqual(defaults["output"], Path("out/"))
        self.assertEqual(defaults["chat_log"], Path("chat.json"))
        self.assertEqual(defaults["weights"], Path("w.json"))  # mapped via _KEY_TO_DEST

    def test_inline_weights_returned_separately(self) -> None:
        weights_dict = {
            "detectors": [{"name": "audio_rms", "weight": 1.0}],
            "fusion": "weighted_sum",
        }
        p = _write({"detector": "composite", "weights": weights_dict})
        try:
            defaults, weights = load_settings(p)
        finally:
            p.unlink()
        self.assertEqual(defaults["detector"], "composite")
        self.assertNotIn("weights", defaults)  # inline weights are NOT in defaults
        self.assertEqual(weights, weights_dict)

    def test_inline_and_path_conflict_raises(self) -> None:
        p = _write({
            "weights": {"detectors": []},
            "weights_path": "w.json",
        })
        try:
            with self.assertRaises(SettingsLoadError):
                load_settings(p)
        finally:
            p.unlink()

    def test_unknown_keys_ignored(self) -> None:
        p = _write({
            "input": "x.mp4",
            "_comment_anything": "free-form notes are fine",
            "future_field": 42,
        })
        try:
            defaults, _ = load_settings(p)
        finally:
            p.unlink()
        self.assertEqual(defaults["input"], Path("x.mp4"))
        self.assertNotIn("_comment_anything", defaults)
        self.assertNotIn("future_field", defaults)

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(SettingsLoadError):
            load_settings(Path("/nonexistent/settings.json"))

    def test_invalid_json_raises(self) -> None:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        f.write("{not valid")
        f.close()
        try:
            with self.assertRaises(SettingsLoadError):
                load_settings(Path(f.name))
        finally:
            Path(f.name).unlink()

    def test_top_level_must_be_object(self) -> None:
        p = _write([1, 2, 3])
        try:
            with self.assertRaises(SettingsLoadError):
                load_settings(p)
        finally:
            p.unlink()

    def test_inline_weights_must_be_object(self) -> None:
        p = _write({"weights": "not an object"})
        try:
            with self.assertRaises(SettingsLoadError):
                load_settings(p)
        finally:
            p.unlink()

    def test_typed_fields_pass_through(self) -> None:
        p = _write({
            "candidates": 4,
            "window": 25.5,
            "min_duration": 5,
            "max_duration": 90.0,
            "export_clips": True,
            "debug": False,
            "detector": "audio_rms",
        })
        try:
            defaults, _ = load_settings(p)
        finally:
            p.unlink()
        self.assertEqual(defaults["candidates"], 4)
        self.assertEqual(defaults["window"], 25.5)
        self.assertEqual(defaults["min_duration"], 5)
        self.assertEqual(defaults["max_duration"], 90.0)
        self.assertTrue(defaults["export_clips"])
        self.assertFalse(defaults["debug"])
        self.assertEqual(defaults["detector"], "audio_rms")


if __name__ == "__main__":
    unittest.main()
