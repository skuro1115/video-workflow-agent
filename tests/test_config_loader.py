"""Tests for src.config_loader."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.config_loader import (
    DEFAULT_DIR_INCLUDE,
    ConfigLoadError,
    load_config,
    parse_config,
)


def _yaml_to_tmp(text: str) -> Path:
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    f.write(text)
    f.flush()
    f.close()
    return Path(f.name)


class ParseConfigDefaultsTests(unittest.TestCase):
    """An empty config should produce all-default values."""

    def test_empty_dict_yields_defaults(self):
        cfg = parse_config({})
        self.assertEqual(cfg.paths.inbox, Path("./inbox"))
        self.assertEqual(cfg.paths.output, Path("./output"))
        self.assertEqual(cfg.paths.archive, Path("./archive"))
        self.assertEqual(cfg.paths.failed, Path("./failed"))
        self.assertEqual(cfg.naming.dir.include, DEFAULT_DIR_INCLUDE)
        self.assertEqual(cfg.naming.dir.separator, "_")
        self.assertEqual(cfg.defaults.detector, "even")
        self.assertEqual(cfg.defaults.candidates, 6)
        self.assertEqual(cfg.defaults.export_clips, False)
        self.assertIsNone(cfg.defaults.weights)

    def test_partial_overrides_keep_other_defaults(self):
        """Setting one nested field shouldn't blow away its siblings."""
        cfg = parse_config({"defaults": {"detector": "audio_rms"}})
        self.assertEqual(cfg.defaults.detector, "audio_rms")
        # Other defaults intact.
        self.assertEqual(cfg.defaults.candidates, 6)
        self.assertEqual(cfg.defaults.window, 30.0)


class ParseConfigPathsTests(unittest.TestCase):
    def test_paths_section_overrides(self):
        cfg = parse_config({"paths": {"inbox": "/tmp/in", "output": "/tmp/out"}})
        self.assertEqual(cfg.paths.inbox, Path("/tmp/in"))
        self.assertEqual(cfg.paths.output, Path("/tmp/out"))
        # Unspecified paths keep defaults.
        self.assertEqual(cfg.paths.archive, Path("./archive"))

    def test_paths_type_error(self):
        with self.assertRaises(ConfigLoadError) as ctx:
            parse_config({"paths": {"inbox": 123}})
        self.assertIn("paths.inbox", str(ctx.exception))


class ParseConfigNamingDirTests(unittest.TestCase):
    def test_include_partial_override(self):
        """Setting include.title=True should leave other flags at default."""
        cfg = parse_config(
            {"naming": {"dir": {"include": {"title": True, "task": False}}}}
        )
        self.assertTrue(cfg.naming.dir.include["title"])
        self.assertFalse(cfg.naming.dir.include["task"])
        # Other flags retained.
        self.assertTrue(cfg.naming.dir.include["date"])
        self.assertTrue(cfg.naming.dir.include["streamer"])

    def test_unknown_include_key_rejected(self):
        with self.assertRaises(ConfigLoadError) as ctx:
            parse_config(
                {"naming": {"dir": {"include": {"streemer": True}}}}  # typo
            )
        self.assertIn("streemer", str(ctx.exception))

    def test_all_includes_false_rejected(self):
        with self.assertRaises(ConfigLoadError) as ctx:
            parse_config(
                {"naming": {"dir": {"include": {
                    "date": False, "streamer": False, "purpose": False,
                    "title": False, "detector": False, "task": False,
                }}}}
            )
        self.assertIn("at least one", str(ctx.exception))

    def test_on_conflict_validated(self):
        with self.assertRaises(ConfigLoadError):
            parse_config({"naming": {"dir": {"on_conflict": "explode"}}})

    def test_slug_max_length_validated(self):
        with self.assertRaises(ConfigLoadError):
            parse_config({"naming": {"dir": {"slug_max_length": 0}}})

    def test_slug_max_length_bool_rejected(self):
        """A bool must not slip through as int (Python booleans ARE ints)."""
        with self.assertRaises(ConfigLoadError):
            parse_config({"naming": {"dir": {"slug_max_length": True}}})

    def test_order_validated(self):
        with self.assertRaises(ConfigLoadError) as ctx:
            parse_config({"naming": {"dir": {"order": ["date", "nope"]}}})
        self.assertIn("nope", str(ctx.exception))


class ParseConfigNamingClipTests(unittest.TestCase):
    def test_clip_unknown_include_rejected(self):
        with self.assertRaises(ConfigLoadError):
            parse_config({"naming": {"clip": {"include": {"unknown_key": True}}}})

    def test_clip_all_false_rejected(self):
        with self.assertRaises(ConfigLoadError):
            parse_config({"naming": {"clip": {"include": {
                "index": False, "slug": False, "detector": False, "timestamp": False,
            }}}})

    def test_clip_index_format_string(self):
        cfg = parse_config({"naming": {"clip": {"index_format": "{:03d}"}}})
        self.assertEqual(cfg.naming.clip.index_format, "{:03d}")


class ParseConfigDefaultsBlockTests(unittest.TestCase):
    def test_int_field_rejects_bool(self):
        with self.assertRaises(ConfigLoadError):
            parse_config({"defaults": {"candidates": True}})

    def test_bool_field_rejects_int(self):
        """`export_clips: 1` should NOT be silently coerced to True."""
        with self.assertRaises(ConfigLoadError):
            parse_config({"defaults": {"export_clips": 1}})

    def test_weights_passed_through_as_dict(self):
        cfg = parse_config({"defaults": {"weights": {
            "detectors": [{"name": "audio_rms", "weight": 2.0}],
            "fusion": "rrf",
        }}})
        self.assertIsNotNone(cfg.defaults.weights)
        self.assertEqual(cfg.defaults.weights["fusion"], "rrf")

    def test_weights_explicit_null_ok(self):
        """Explicit `weights: null` is fine — same as omitted."""
        cfg = parse_config({"defaults": {"weights": None}})
        self.assertIsNone(cfg.defaults.weights)

    def test_numeric_field_accepts_int_or_float(self):
        cfg = parse_config({"defaults": {"window": 45, "min_duration": 5.5}})
        self.assertEqual(cfg.defaults.window, 45.0)
        self.assertEqual(cfg.defaults.min_duration, 5.5)


class LoadConfigTests(unittest.TestCase):
    def test_load_full_yaml(self):
        text = """
paths:
  inbox: ./my-inbox
  output: ./my-output
naming:
  dir:
    include:
      title: true
    separator: "-"
defaults:
  detector: audio_rms
  candidates: 10
  weights:
    detectors:
      - {name: audio_rms, weight: 1.5}
    fusion: weighted_sum
"""
        path = _yaml_to_tmp(text)
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.paths.inbox, Path("./my-inbox"))
            self.assertTrue(cfg.naming.dir.include["title"])
            self.assertEqual(cfg.naming.dir.separator, "-")
            self.assertEqual(cfg.defaults.detector, "audio_rms")
            self.assertEqual(cfg.defaults.candidates, 10)
            self.assertEqual(cfg.defaults.weights["fusion"], "weighted_sum")
            self.assertEqual(cfg.source_path, path)
        finally:
            path.unlink()

    def test_load_empty_file_yields_defaults(self):
        path = _yaml_to_tmp("")
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.defaults.detector, "even")
        finally:
            path.unlink()

    def test_load_missing_file_raises(self):
        with self.assertRaises(ConfigLoadError) as ctx:
            load_config(Path("/nonexistent/config.yaml"))
        self.assertIn("not found", str(ctx.exception))

    def test_load_invalid_yaml_raises(self):
        path = _yaml_to_tmp("paths:\n  inbox: [unclosed")
        try:
            with self.assertRaises(ConfigLoadError) as ctx:
                load_config(path)
            self.assertIn("invalid YAML", str(ctx.exception))
        finally:
            path.unlink()

    def test_load_non_mapping_raises(self):
        """Top-level must be a mapping, not a list."""
        path = _yaml_to_tmp("- just\n- a\n- list\n")
        try:
            with self.assertRaises(ConfigLoadError):
                load_config(path)
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main()
