"""Tests for src.inbox (task parsing, discovery, lifecycle, processing)."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src.config_loader import Config, parse_config
from src.inbox import (
    InboxResult,
    Task,
    TaskLoadError,
    TaskResult,
    discover_tasks,
    parse_task,
    process_inbox,
    process_task,
    resolve_relative_to_config,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class ParseTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_minimal_valid(self):
        p = _write(self.root / "foo.task.yaml", "source: ./video.mp4\n")
        t = parse_task(p)
        self.assertEqual(t.name, "foo")
        self.assertEqual(t.source, "./video.mp4")
        self.assertIsNone(t.streamer)

    def test_full_fields(self):
        text = """
source: https://www.youtube.com/watch?v=abc
streamer: streamerA
purpose: funny
title: "【神回】コラボ"
date: 2026-05-15
chat_log: ./chat.json
detector: composite
candidates: 10
window: 45
"""
        p = _write(self.root / "full.task.yaml", text)
        t = parse_task(p)
        self.assertEqual(t.streamer, "streamerA")
        self.assertEqual(t.title, "【神回】コラボ")
        self.assertEqual(t.date, date(2026, 5, 15))
        self.assertEqual(t.chat_log, "./chat.json")
        self.assertEqual(t.overrides["detector"], "composite")
        self.assertEqual(t.overrides["candidates"], 10)
        self.assertEqual(t.overrides["window"], 45)

    def test_missing_source_raises(self):
        p = _write(self.root / "x.task.yaml", "streamer: foo\n")
        with self.assertRaises(TaskLoadError) as ctx:
            parse_task(p)
        self.assertIn("source", str(ctx.exception))

    def test_blank_source_raises(self):
        p = _write(self.root / "x.task.yaml", "source: '   '\n")
        with self.assertRaises(TaskLoadError):
            parse_task(p)

    def test_unknown_key_raises(self):
        """A typo like 'streemer' must fail loudly, not silently drop."""
        p = _write(self.root / "x.task.yaml", "source: v.mp4\nstreemer: foo\n")
        with self.assertRaises(TaskLoadError) as ctx:
            parse_task(p)
        self.assertIn("streemer", str(ctx.exception))

    def test_invalid_yaml_raises(self):
        p = _write(self.root / "x.task.yaml", "source: [unclosed\n")
        with self.assertRaises(TaskLoadError):
            parse_task(p)

    def test_empty_file_raises(self):
        p = _write(self.root / "x.task.yaml", "")
        with self.assertRaises(TaskLoadError):
            parse_task(p)

    def test_date_as_iso_string(self):
        """PyYAML usually parses 2026-05-15 as a date, but quoted strings stay str."""
        p = _write(self.root / "x.task.yaml", 'source: v.mp4\ndate: "2026-12-25"\n')
        t = parse_task(p)
        self.assertEqual(t.date, date(2026, 12, 25))

    def test_invalid_date_raises(self):
        p = _write(self.root / "x.task.yaml", 'source: v.mp4\ndate: "not-a-date"\n')
        with self.assertRaises(TaskLoadError):
            parse_task(p)

    def test_streamer_must_be_string(self):
        p = _write(self.root / "x.task.yaml", "source: v.mp4\nstreamer: 123\n")
        with self.assertRaises(TaskLoadError):
            parse_task(p)

    def test_task_name_strips_dot_task(self):
        """Stem of `foo.task.yaml` is `foo.task`; trim to `foo`."""
        p = _write(self.root / "foo.task.yaml", "source: v.mp4\n")
        t = parse_task(p)
        self.assertEqual(t.name, "foo")


class DiscoverTasksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_task_yaml_files(self):
        _write(self.root / "a.task.yaml", "source: v.mp4\n")
        _write(self.root / "b.task.yaml", "source: v.mp4\n")
        _write(self.root / "c.task.yml", "source: v.mp4\n")  # .yml also picked up
        _write(self.root / "not-a-task.txt", "ignored")
        _write(self.root / "config.yaml", "ignored")  # missing .task
        paths = discover_tasks(self.root)
        names = sorted(p.name for p in paths)
        self.assertEqual(names, ["a.task.yaml", "b.task.yaml", "c.task.yml"])

    def test_missing_dir_returns_empty(self):
        self.assertEqual(discover_tasks(self.root / "nope"), [])

    def test_sorted_deterministically(self):
        _write(self.root / "z.task.yaml", "source: v.mp4\n")
        _write(self.root / "a.task.yaml", "source: v.mp4\n")
        _write(self.root / "m.task.yaml", "source: v.mp4\n")
        paths = discover_tasks(self.root)
        names = [p.name for p in paths]
        self.assertEqual(names, ["a.task.yaml", "m.task.yaml", "z.task.yaml"])


class ResolveRelativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolves_relative_to_config_dir(self):
        cfg_path = self.root / "config.yaml"
        cfg_path.write_text("")
        cfg = Config(source_path=cfg_path)
        p = resolve_relative_to_config(cfg, Path("./inbox"))
        self.assertEqual(p, self.root / "inbox")

    def test_absolute_passes_through(self):
        cfg = Config(source_path=self.root / "config.yaml")
        abs_path = Path("/tmp/abs/inbox").resolve()
        self.assertEqual(resolve_relative_to_config(cfg, abs_path), abs_path)


class ProcessTaskTests(unittest.TestCase):
    """Pipeline is mocked — these test the inbox orchestration only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.cfg_path = self.root / "config.yaml"
        self.cfg_path.write_text("")
        self.cfg = parse_config({})
        self.cfg.source_path = self.cfg_path
        # Pre-create a fake input file the task can point at.
        self.video_path = self.root / "video.mp4"
        self.video_path.write_bytes(b"\x00")

    def tearDown(self):
        self.tmp.cleanup()

    def _make_task(self, **overrides) -> Task:
        return Task(
            name="t1",
            source_path=self.root / "t1.task.yaml",
            source=str(self.video_path),
            streamer="strA",
            purpose="funny",
            **overrides,
        )

    def test_success_returns_output_dir(self):
        with patch("src.main.run", return_value=0) as mock_run:
            result = process_task(self.cfg, self._make_task())
        self.assertEqual(result.status, "success")
        self.assertIsNotNone(result.output_dir)
        # Default naming: date_strA_funny_t1
        self.assertIn("strA", result.output_dir.name)
        self.assertIn("funny", result.output_dir.name)
        self.assertIn("t1", result.output_dir.name)
        mock_run.assert_called_once()

    def test_missing_source_video_is_caught(self):
        task = self._make_task()
        task.source = str(self.root / "does-not-exist.mp4")
        with patch("src.main.run", return_value=0):
            result = process_task(self.cfg, task)
        self.assertEqual(result.status, "failed")
        self.assertIn("not found", result.error)

    def test_pipeline_nonzero_rc_is_failed(self):
        with patch("src.main.run", return_value=5):
            result = process_task(self.cfg, self._make_task())
        self.assertEqual(result.status, "failed")
        self.assertIn("5", result.error)

    def test_pipeline_exception_caught(self):
        with patch("src.main.run", side_effect=RuntimeError("boom")):
            result = process_task(self.cfg, self._make_task())
        self.assertEqual(result.status, "failed")
        self.assertIn("boom", result.error)
        self.assertIsNotNone(result.error_detail)

    def test_task_overrides_reach_pipeline_config(self):
        task = self._make_task()
        task.overrides = {"detector": "audio_rms", "candidates": 12, "window": 45}
        captured = {}

        def fake_run(pcfg, **kwargs):
            captured["cfg"] = pcfg
            return 0

        with patch("src.main.run", side_effect=fake_run):
            process_task(self.cfg, task)
        self.assertEqual(captured["cfg"].detector, "audio_rms")
        self.assertEqual(captured["cfg"].candidate_count, 12)
        self.assertEqual(captured["cfg"].candidate_duration, 45.0)

    def test_conflict_resolution_picks_suffix(self):
        """If the computed dir already exists and task is included by default,
        suffix path kicks in (task=true makes conflicts unlikely but still
        handled by the engine)."""
        # Disable task component so the conflict path is exercised.
        self.cfg.naming.dir.include["task"] = False
        # Pre-create the base dir.
        (self.root / "output").mkdir()
        comps_dir_name = None
        with patch("src.main.run", return_value=0):
            # First run creates the base dir name.
            r1 = process_task(self.cfg, self._make_task())
            comps_dir_name = r1.output_dir.name
            # Mark the dir as existing for the second run.
            r1.output_dir.mkdir(exist_ok=True)
            r2 = process_task(self.cfg, self._make_task())
        self.assertEqual(r2.output_dir.name, f"{comps_dir_name}_2")

    def test_chat_log_resolved_relative_to_config(self):
        chat = self.root / "chat.json"
        chat.write_text("[]")
        task = self._make_task()
        task.chat_log = "./chat.json"
        captured = {}

        def fake_run(pcfg, **kwargs):
            captured["chat"] = kwargs.get("chat_log_path")
            return 0

        with patch("src.main.run", side_effect=fake_run):
            process_task(self.cfg, task)
        self.assertEqual(captured["chat"], chat)


class ProcessInboxLifecycleTests(unittest.TestCase):
    """End-to-end: task file goes from inbox → archive (or failed)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.cfg_path = self.root / "config.yaml"
        self.cfg_path.write_text("")
        self.cfg = parse_config({
            "paths": {
                "inbox": str(self.root / "inbox"),
                "output": str(self.root / "output"),
                "archive": str(self.root / "archive"),
                "failed": str(self.root / "failed"),
            }
        })
        self.cfg.source_path = self.cfg_path
        self.video = self.root / "video.mp4"
        self.video.write_bytes(b"\x00")

    def tearDown(self):
        self.tmp.cleanup()

    def _make_task_file(self, name: str = "t1", source: str | None = None) -> Path:
        text = f"source: {source or self.video}\nstreamer: strA\npurpose: funny\n"
        return _write(self.root / "inbox" / f"{name}.task.yaml", text)

    def test_success_moves_to_archive(self):
        task_path = self._make_task_file()
        with patch("src.main.run", return_value=0):
            result = process_inbox(self.cfg)
        self.assertEqual(result.total, 1)
        self.assertEqual(result.succeeded, 1)
        self.assertEqual(result.failed, 0)
        self.assertFalse(task_path.exists())
        self.assertTrue((self.root / "archive" / "t1.task.yaml").exists())

    def test_failure_moves_to_failed_with_log(self):
        task_path = self._make_task_file()
        with patch("src.main.run", side_effect=RuntimeError("explode")):
            result = process_inbox(self.cfg)
        self.assertEqual(result.failed, 1)
        self.assertFalse(task_path.exists())
        failed_yaml = self.root / "failed" / "t1.task.yaml"
        self.assertTrue(failed_yaml.exists())
        log = self.root / "failed" / "t1.task.yaml.error.log"
        self.assertTrue(log.exists())
        self.assertIn("explode", log.read_text())

    def test_malformed_task_goes_to_failed(self):
        """Even a YAML parse error funnels into the failed/ pile."""
        bad = _write(self.root / "inbox" / "bad.task.yaml", "source: [unclosed\n")
        with patch("src.main.run", return_value=0):
            result = process_inbox(self.cfg)
        self.assertEqual(result.failed, 1)
        self.assertFalse(bad.exists())
        self.assertTrue((self.root / "failed" / "bad.task.yaml").exists())

    def test_specific_task_name_filters(self):
        self._make_task_file("alpha")
        self._make_task_file("beta")
        with patch("src.main.run", return_value=0):
            result = process_inbox(self.cfg, task_name="alpha")
        self.assertEqual(result.total, 1)
        # beta untouched.
        self.assertTrue((self.root / "inbox" / "beta.task.yaml").exists())

    def test_dry_run_doesnt_move_files(self):
        task_path = self._make_task_file()
        with patch("src.main.run", return_value=0) as mock_run:
            result = process_inbox(self.cfg, dry_run=True)
        self.assertEqual(result.skipped, 1)
        self.assertTrue(task_path.exists())   # NOT moved
        mock_run.assert_not_called()

    def test_archive_collision_gets_suffix(self):
        """Re-running an already-archived name shouldn't clobber the archive."""
        # Pre-populate archive with the same name.
        (self.root / "archive").mkdir()
        (self.root / "archive" / "t1.task.yaml").write_text("# old\n")
        self._make_task_file()
        with patch("src.main.run", return_value=0):
            process_inbox(self.cfg)
        # Old archive preserved; new entry got suffixed.
        self.assertEqual((self.root / "archive" / "t1.task.yaml").read_text(), "# old\n")
        self.assertTrue((self.root / "archive" / "t1_2.task.yaml").exists())

    def test_empty_inbox_succeeds(self):
        # inbox dir doesn't even exist
        result = process_inbox(self.cfg)
        self.assertEqual(result.total, 0)
        self.assertEqual(result.succeeded, 0)


class ClipRenameTests(unittest.TestCase):
    """Verify clip files get renamed according to naming.clip when a plan exists."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.cfg_path = self.root / "config.yaml"
        self.cfg_path.write_text("")
        self.cfg = parse_config({})
        self.cfg.source_path = self.cfg_path
        self.video = self.root / "video.mp4"
        self.video.write_bytes(b"\x00")

    def tearDown(self):
        self.tmp.cleanup()

    def _task(self) -> Task:
        return Task(
            name="t1", source_path=self.root / "t1.task.yaml",
            source=str(self.video), streamer="strA", purpose="funny",
            title="my-cool-clip",
        )

    def test_clip_files_renamed_after_pipeline(self):
        def fake_run(pcfg, **kwargs):
            # Simulate the pipeline writing a plan + clip + thumbnail.
            pcfg.output_dir.mkdir(parents=True, exist_ok=True)
            (pcfg.output_dir / "clips").mkdir()
            (pcfg.output_dir / "thumbnails").mkdir()
            plan = [{"clip_id": "clip_01", "source_start": 120.0,
                     "source_end": 150.0, "duration": 30.0}]
            (pcfg.output_dir / "clip_plan.json").write_text(json.dumps(plan))
            (pcfg.output_dir / "clips" / "clip_01.mp4").write_bytes(b"\x00")
            (pcfg.output_dir / "thumbnails" / "clip_01.jpg").write_bytes(b"\x00")
            return 0

        with patch("src.main.run", side_effect=fake_run):
            result = process_task(self.cfg, self._task())
        self.assertEqual(result.status, "success")
        # New name: 01_my-cool-clip.{mp4,jpg}
        self.assertTrue((result.output_dir / "clips" / "01_my-cool-clip.mp4").exists())
        self.assertTrue((result.output_dir / "thumbnails" / "01_my-cool-clip.jpg").exists())


if __name__ == "__main__":
    unittest.main()
