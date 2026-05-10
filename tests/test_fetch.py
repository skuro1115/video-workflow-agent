"""Unit tests for scripts/fetch.py — purely format conversion (no network)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from scripts import fetch


class TestDetectPlatform(unittest.TestCase):
    def test_youtube_long(self):
        self.assertEqual(
            fetch.detect_platform("https://www.youtube.com/watch?v=abc123"),
            "youtube",
        )

    def test_youtube_short(self):
        self.assertEqual(fetch.detect_platform("https://youtu.be/abc123"), "youtube")

    def test_youtube_mobile(self):
        self.assertEqual(
            fetch.detect_platform("https://m.youtube.com/watch?v=abc123"),
            "youtube",
        )

    def test_twitch(self):
        self.assertEqual(
            fetch.detect_platform("https://www.twitch.tv/videos/123456"),
            "twitch",
        )

    def test_unsupported(self):
        with self.assertRaises(ValueError):
            fetch.detect_platform("https://vimeo.com/123")

    def test_not_url(self):
        with self.assertRaises(ValueError):
            fetch.detect_platform("just a string")


class TestParseYoutubeLiveChat(unittest.TestCase):
    def _entry(self, *, offset_msec, user, runs, wrap_in_replay=True):
        renderer = {
            "liveChatTextMessageRenderer": {
                "message": {"runs": runs},
                "authorName": {"simpleText": user},
                "timestampUsec": "0",
            }
        }
        action = {"addChatItemAction": {"item": renderer}}
        if wrap_in_replay:
            return {
                "replayChatItemAction": {
                    "actions": [action],
                    "videoOffsetTimeMsec": str(offset_msec),
                },
                "videoOffsetTimeMsec": str(offset_msec),
            }
        return {
            "videoOffsetTimeMsec": str(offset_msec),
            "addChatItemAction": {"item": renderer},
        }

    def test_text_message(self):
        line = json.dumps(self._entry(
            offset_msec=12500,
            user="alice",
            runs=[{"text": "hello"}, {"text": " world"}],
        ))
        result = fetch.parse_youtube_live_chat_jsonl([line])
        self.assertEqual(
            result,
            [{"t": 12.5, "user": "alice", "text": "hello world"}],
        )

    def test_emoji_run_with_shortcut(self):
        line = json.dumps(self._entry(
            offset_msec=1000,
            user="bob",
            runs=[{"text": "lol "}, {"emoji": {"shortcuts": [":joy:"]}}],
        ))
        result = fetch.parse_youtube_live_chat_jsonl([line])
        self.assertEqual(result[0]["text"], "lol :joy:")

    def test_skip_blank_and_invalid_lines(self):
        good = json.dumps(self._entry(
            offset_msec=5000, user="x", runs=[{"text": "hi"}],
        ))
        lines = ["", "   ", "{not json", good]
        result = fetch.parse_youtube_live_chat_jsonl(lines)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "hi")

    def test_skip_entries_without_text_renderer(self):
        # Membership / sticker / paid messages with no text renderer
        non_text = json.dumps({
            "videoOffsetTimeMsec": "1000",
            "replayChatItemAction": {
                "actions": [{
                    "addChatItemAction": {
                        "item": {"liveChatPaidStickerRenderer": {}}
                    }
                }],
            },
        })
        good = json.dumps(self._entry(
            offset_msec=2000, user="x", runs=[{"text": "ok"}],
        ))
        result = fetch.parse_youtube_live_chat_jsonl([non_text, good])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "ok")

    def test_sorted_by_timestamp(self):
        lines = [
            json.dumps(self._entry(offset_msec=3000, user="c", runs=[{"text": "third"}])),
            json.dumps(self._entry(offset_msec=1000, user="a", runs=[{"text": "first"}])),
            json.dumps(self._entry(offset_msec=2000, user="b", runs=[{"text": "second"}])),
        ]
        result = fetch.parse_youtube_live_chat_jsonl(lines)
        self.assertEqual([m["text"] for m in result], ["first", "second", "third"])

    def test_anonymous_when_no_author(self):
        entry = self._entry(offset_msec=1000, user="", runs=[{"text": "hi"}])
        # Strip the simpleText to simulate a missing name
        entry["replayChatItemAction"]["actions"][0]["addChatItemAction"]["item"][
            "liveChatTextMessageRenderer"
        ]["authorName"] = {}
        result = fetch.parse_youtube_live_chat_jsonl([json.dumps(entry)])
        self.assertEqual(result[0]["user"], "anonymous")

    def test_skip_when_text_only_whitespace(self):
        entry = self._entry(offset_msec=1000, user="x", runs=[{"text": "   "}])
        result = fetch.parse_youtube_live_chat_jsonl([json.dumps(entry)])
        self.assertEqual(result, [])


class TestParseTwitchChat(unittest.TestCase):
    def test_basic(self):
        raw = [
            {"time_in_seconds": 12.34, "author": {"name": "alice"}, "message": "hello"},
            {"time_in_seconds": 15.0, "author": {"display_name": "Bob"}, "message": "hi"},
        ]
        result = fetch.parse_twitch_chat_json(raw)
        self.assertEqual(result, [
            {"t": 12.34, "user": "alice", "text": "hello"},
            {"t": 15.0, "user": "Bob", "text": "hi"},
        ])

    def test_display_name_preferred_over_name(self):
        raw = [{
            "time_in_seconds": 1.0,
            "author": {"name": "lower", "display_name": "Upper"},
            "message": "x",
        }]
        result = fetch.parse_twitch_chat_json(raw)
        self.assertEqual(result[0]["user"], "Upper")

    def test_skip_messages_without_time(self):
        raw = [
            {"author": {"name": "a"}, "message": "no time"},
            {"time_in_seconds": 1.0, "author": {"name": "b"}, "message": "ok"},
        ]
        result = fetch.parse_twitch_chat_json(raw)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["user"], "b")

    def test_skip_empty_message(self):
        raw = [
            {"time_in_seconds": 1.0, "author": {"name": "a"}, "message": ""},
            {"time_in_seconds": 2.0, "author": {"name": "b"}, "message": "   "},
            {"time_in_seconds": 3.0, "author": {"name": "c"}, "message": "ok"},
        ]
        result = fetch.parse_twitch_chat_json(raw)
        self.assertEqual([m["user"] for m in result], ["c"])

    def test_anonymous_when_no_author(self):
        raw = [{"time_in_seconds": 1.0, "message": "hi"}]
        self.assertEqual(fetch.parse_twitch_chat_json(raw)[0]["user"], "anonymous")

    def test_sorted(self):
        raw = [
            {"time_in_seconds": 5.0, "author": {"name": "a"}, "message": "later"},
            {"time_in_seconds": 1.0, "author": {"name": "b"}, "message": "earlier"},
        ]
        result = fetch.parse_twitch_chat_json(raw)
        self.assertEqual([m["text"] for m in result], ["earlier", "later"])

    def test_non_list_input(self):
        self.assertEqual(fetch.parse_twitch_chat_json({"oops": True}), [])


class TestDeriveName(unittest.TestCase):
    def test_youtube_v_param(self):
        self.assertEqual(
            fetch._derive_name("https://www.youtube.com/watch?v=abc123XYZ&t=42"),
            "abc123XYZ",
        )

    def test_path_tail_fallback(self):
        self.assertEqual(
            fetch._derive_name("https://www.twitch.tv/videos/987654"),
            "987654",
        )

    def test_sanitises_unsafe_chars(self):
        self.assertEqual(
            fetch._derive_name("https://example.com/foo bar?baz"),
            "foo_bar_baz",
        )


class TestRequireCommand(unittest.TestCase):
    def test_raises_when_missing(self):
        with mock.patch("scripts.fetch.shutil.which", return_value=None):
            with self.assertRaises(fetch.FetchError) as ctx:
                fetch._require_command("nope", "pip install nope", exit_code=99)
        self.assertEqual(ctx.exception.exit_code, 99)


if __name__ == "__main__":
    unittest.main()
