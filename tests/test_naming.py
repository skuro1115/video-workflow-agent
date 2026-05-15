"""Tests for src.naming (slug, dir/clip render, conflict resolution)."""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.config_loader import NamingClipConfig, NamingDirConfig
from src.naming import (
    ClipComponents,
    DirComponents,
    render_clip_name,
    render_dir_name,
    resolve_output_dir,
    slugify,
)


class SlugifyTests(unittest.TestCase):
    def test_basic_ascii(self):
        self.assertEqual(slugify("hello world"), "hello-world")

    def test_japanese_preserved(self):
        """Japanese chars are filesystem-safe; keep them in slug."""
        self.assertEqual(slugify("神回コラボ"), "神回コラボ")

    def test_collapses_separators(self):
        self.assertEqual(slugify("foo___bar---baz   qux"), "foo-bar-baz-qux")

    def test_strips_unsafe_path_chars(self):
        self.assertEqual(slugify('foo/bar:baz*qux?'), "foo-bar-baz-qux")

    def test_strips_outer_dashes(self):
        self.assertEqual(slugify("---hello---"), "hello")

    def test_empty_becomes_untitled(self):
        self.assertEqual(slugify(""), "untitled")
        self.assertEqual(slugify("///"), "untitled")
        self.assertEqual(slugify("   "), "untitled")

    def test_truncates_to_max_length(self):
        s = slugify("a" * 100, max_length=20)
        self.assertEqual(s, "a" * 20)

    def test_truncate_doesnt_leave_trailing_dash(self):
        """Truncating in the middle of a separator run shouldn't leak a dash."""
        s = slugify("aaaa----bbbb", max_length=6)
        # "aaaa-b" → after rstrip("-") stays "aaaa-b"; the case to guard is
        # when truncation falls on the dash boundary: "aaaa-" → "aaaa".
        s2 = slugify("aaaa----bbbb", max_length=5)
        self.assertFalse(s2.endswith("-"))


class RenderDirNameTests(unittest.TestCase):
    def setUp(self):
        # Default config from config_loader's NamingDirConfig() factory.
        self.cfg = NamingDirConfig()

    def test_default_includes_date_streamer_purpose_task(self):
        """include.title default off; include.task default on."""
        name = render_dir_name(self.cfg, DirComponents(
            date=date(2026, 5, 15),
            streamer="streamerA",
            purpose="funny",
            title="this title should not appear",
            task="2026-05-15-streamerA-funny",
        ))
        # date, streamer, purpose, task — in order, no title.
        self.assertEqual(
            name, "2026-05-15_streamerA_funny_2026-05-15-streamerA-funny"
        )

    def test_turning_title_on_adds_title_in_order(self):
        self.cfg.include["title"] = True
        name = render_dir_name(self.cfg, DirComponents(
            date=date(2026, 5, 15),
            streamer="streamerA",
            purpose="funny",
            title="神回コラボ",
            task="t1",
        ))
        self.assertEqual(name, "2026-05-15_streamerA_funny_神回コラボ_t1")

    def test_turning_streamer_off_omits_it(self):
        self.cfg.include["streamer"] = False
        name = render_dir_name(self.cfg, DirComponents(
            date=date(2026, 5, 15),
            streamer="streamerA",       # supplied but disabled
            purpose="funny",
            task="t1",
        ))
        self.assertEqual(name, "2026-05-15_funny_t1")

    def test_missing_value_silently_skipped(self):
        """Include is on but task didn't supply the value — skip, don't error."""
        name = render_dir_name(self.cfg, DirComponents(
            date=date(2026, 5, 15),
            streamer=None,            # missing
            purpose="funny",
            task="t1",
        ))
        self.assertEqual(name, "2026-05-15_funny_t1")

    def test_empty_string_is_skipped(self):
        name = render_dir_name(self.cfg, DirComponents(
            date=date(2026, 5, 15),
            streamer="",              # empty string == missing
            purpose="funny",
            task="t1",
        ))
        self.assertEqual(name, "2026-05-15_funny_t1")

    def test_no_components_resolve_raises(self):
        """If date is off and other components are missing → empty → error.

        ``date`` falls back to today's date so it always renders something.
        Disabling it lets us actually reach the empty-name guard.
        """
        self.cfg.include["date"] = False
        with self.assertRaises(ValueError):
            render_dir_name(self.cfg, DirComponents(
                streamer=None, purpose=None, task=None,
            ))

    def test_custom_separator(self):
        self.cfg.separator = "-"
        name = render_dir_name(self.cfg, DirComponents(
            date=date(2026, 5, 15),
            streamer="strA",
            purpose="funny",
            task="t1",
        ))
        self.assertEqual(name, "2026-05-15-strA-funny-t1")

    def test_custom_date_format(self):
        self.cfg.date_format = "%Y%m%d"
        name = render_dir_name(self.cfg, DirComponents(
            date=date(2026, 5, 15),
            streamer="strA",
            purpose="funny",
            task="t1",
        ))
        self.assertEqual(name, "20260515_strA_funny_t1")

    def test_order_drives_layout(self):
        """Reorder so task appears first; the rendered string follows order."""
        self.cfg.order = ["task", "date", "streamer", "purpose", "title", "detector"]
        name = render_dir_name(self.cfg, DirComponents(
            date=date(2026, 5, 15),
            streamer="strA",
            purpose="funny",
            task="t1",
        ))
        self.assertEqual(name, "t1_2026-05-15_strA_funny")

    def test_component_not_in_order_is_dropped(self):
        """If a component is enabled but absent from order, it doesn't appear."""
        self.cfg.order = ["date", "task"]  # purpose missing from order
        name = render_dir_name(self.cfg, DirComponents(
            date=date(2026, 5, 15),
            purpose="funny",   # would appear, but order excludes it
            task="t1",
        ))
        self.assertEqual(name, "2026-05-15_t1")

    def test_detector_component(self):
        self.cfg.include["detector"] = True
        name = render_dir_name(self.cfg, DirComponents(
            date=date(2026, 5, 15),
            streamer="strA",
            purpose="funny",
            detector="composite",
            task="t1",
        ))
        self.assertEqual(name, "2026-05-15_strA_funny_composite_t1")


class ResolveOutputDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = NamingDirConfig()

    def tearDown(self):
        self.tmp.cleanup()

    def _make_comps(self) -> DirComponents:
        return DirComponents(
            date=date(2026, 5, 15),
            streamer="strA",
            purpose="funny",
            task="t1",
        )

    def test_first_run_no_conflict(self):
        p = resolve_output_dir(self.cfg, self._make_comps(), self.root)
        self.assertEqual(p.name, "2026-05-15_strA_funny_t1")
        self.assertFalse(p.exists())  # resolve doesn't create

    def test_suffix_on_conflict(self):
        # Pre-create the base dir to force a conflict.
        base = self.root / "2026-05-15_strA_funny_t1"
        base.mkdir()
        p = resolve_output_dir(self.cfg, self._make_comps(), self.root)
        self.assertEqual(p.name, "2026-05-15_strA_funny_t1_2")

    def test_suffix_increments_until_free(self):
        base = self.root / "2026-05-15_strA_funny_t1"
        base.mkdir()
        (self.root / "2026-05-15_strA_funny_t1_2").mkdir()
        (self.root / "2026-05-15_strA_funny_t1_3").mkdir()
        p = resolve_output_dir(self.cfg, self._make_comps(), self.root)
        self.assertEqual(p.name, "2026-05-15_strA_funny_t1_4")

    def test_on_conflict_error_raises(self):
        self.cfg.on_conflict = "error"
        base = self.root / "2026-05-15_strA_funny_t1"
        base.mkdir()
        with self.assertRaises(FileExistsError):
            resolve_output_dir(self.cfg, self._make_comps(), self.root)


class RenderClipNameTests(unittest.TestCase):
    def setUp(self):
        self.cfg = NamingClipConfig()

    def test_default_index_and_slug(self):
        name = render_clip_name(self.cfg, ClipComponents(
            index=1,
            slug="kamikai-saikyou-collab",
        ))
        self.assertEqual(name, "01_kamikai-saikyou-collab")

    def test_index_zero_padding_from_format(self):
        self.cfg.index_format = "{:03d}"
        name = render_clip_name(self.cfg, ClipComponents(index=7, slug="foo"))
        self.assertEqual(name, "007_foo")

    def test_timestamp_short_form(self):
        self.cfg.include["timestamp"] = True
        name = render_clip_name(self.cfg, ClipComponents(
            index=1, slug="foo", timestamp_seconds=125.4
        ))
        # 125s → 02m05s
        self.assertEqual(name, "01_foo_02m05s")

    def test_timestamp_with_hours(self):
        self.cfg.include["timestamp"] = True
        name = render_clip_name(self.cfg, ClipComponents(
            index=1, slug="foo", timestamp_seconds=3725.0
        ))
        self.assertEqual(name, "01_foo_1h02m05s")

    def test_detector_component(self):
        self.cfg.include["detector"] = True
        name = render_clip_name(self.cfg, ClipComponents(
            index=1, slug="foo", detector="composite"
        ))
        self.assertEqual(name, "01_foo_composite")

    def test_slug_off_index_only(self):
        self.cfg.include["slug"] = False
        name = render_clip_name(self.cfg, ClipComponents(index=3, slug="ignored"))
        self.assertEqual(name, "03")

    def test_missing_slug_falls_through(self):
        name = render_clip_name(self.cfg, ClipComponents(index=1, slug=None))
        self.assertEqual(name, "01")

    def test_all_missing_raises(self):
        with self.assertRaises(ValueError):
            render_clip_name(self.cfg, ClipComponents())


if __name__ == "__main__":
    unittest.main()
