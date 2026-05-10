"""Fetch a video + chat log from a URL into the app's expected formats.

Usage::

    # YouTube live archive
    python -m scripts.fetch \\
        --url https://www.youtube.com/watch?v=XXXXXXXXXXX \\
        --output samples/ --name liveA

    # Twitch VOD
    python -m scripts.fetch \\
        --url https://www.twitch.tv/videos/123456789 \\
        --output samples/ --name vodA

Outputs (under ``--output``)::

    <name>.mp4          # downloaded video (yt-dlp)
    <name>.chat.json    # converted into the app's [{t, user, text}] format

If ``--name`` is omitted, the source video ID is used.

External commands required:
  - ``yt-dlp``         (video + YouTube live-chat replay)
  - ``chat-downloader`` (Twitch VOD chat — yt-dlp does not extract it)

Exit codes:
  0  success
  20 unsupported URL (not YouTube / Twitch)
  21 yt-dlp not found on PATH
  22 yt-dlp failed (download or chat extraction)
  23 chat-downloader not found on PATH
  24 chat-downloader failed
  25 chat file expected but not produced (e.g. no live chat replay)
  26 chat parse failed (raw file present but malformed)
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

_YOUTUBE_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be")
_TWITCH_HOSTS = ("twitch.tv", "www.twitch.tv", "m.twitch.tv")


def detect_platform(url: str) -> str:
    """Return ``"youtube"`` or ``"twitch"`` for a URL, or raise ``ValueError``.

    Detection is host-only — does not validate that the URL points at a
    playable resource.
    """
    m = re.match(r"^https?://([^/]+)/", url + "/")
    if not m:
        raise ValueError(f"not a recognisable URL: {url!r}")
    host = m.group(1).lower()
    if host in _YOUTUBE_HOSTS:
        return "youtube"
    if host in _TWITCH_HOSTS:
        return "twitch"
    raise ValueError(f"unsupported host: {host!r} (only youtube / twitch are supported)")


# ---------------------------------------------------------------------------
# Chat format converters (pure functions — no subprocess, fully testable)
# ---------------------------------------------------------------------------


def _walk_for_text_renderer(node) -> dict | None:
    """Find a ``liveChatTextMessageRenderer`` dict anywhere under ``node``.

    yt-dlp's live_chat JSONL nests the renderer differently depending on the
    chat item type (text, paid message, sticker, member, etc.). We only care
    about plain text messages — walking the tree and matching on the renderer
    key is more robust than hard-coding a path.
    """
    if isinstance(node, dict):
        if "liveChatTextMessageRenderer" in node:
            return node["liveChatTextMessageRenderer"]
        for v in node.values():
            r = _walk_for_text_renderer(v)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _walk_for_text_renderer(v)
            if r is not None:
                return r
    return None


def _extract_text_from_runs(message: dict) -> str:
    """Concatenate the ``runs`` array of a YouTube message into a plain string.

    Emoji runs (which lack ``text``) are mapped to their ``:shortcut:`` form
    when present, otherwise dropped.
    """
    runs = message.get("runs") or []
    out: list[str] = []
    for run in runs:
        if "text" in run:
            out.append(str(run["text"]))
        elif "emoji" in run:
            emoji = run["emoji"]
            shortcuts = emoji.get("shortcuts") or []
            if shortcuts:
                out.append(str(shortcuts[0]))
            elif "emojiId" in emoji:
                out.append(f":{emoji['emojiId']}:")
    return "".join(out)


def _youtube_entry_to_message(entry: dict) -> dict | None:
    """Convert one yt-dlp live_chat JSONL entry to ``{t, user, text}``.

    Returns ``None`` for entries that don't represent a plain text message
    (system events, super chats with no text, paid stickers, etc.) — caller
    should drop these.
    """
    offset_msec = entry.get("videoOffsetTimeMsec")
    if offset_msec is None:
        replay = entry.get("replayChatItemAction")
        if isinstance(replay, dict):
            offset_msec = replay.get("videoOffsetTimeMsec")
    if offset_msec is None:
        return None
    try:
        t = float(offset_msec) / 1000.0
    except (TypeError, ValueError):
        return None

    renderer = _walk_for_text_renderer(entry)
    if renderer is None:
        return None
    message = renderer.get("message")
    if not isinstance(message, dict):
        return None
    text = _extract_text_from_runs(message).strip()
    if not text:
        return None
    author = renderer.get("authorName") or {}
    user = str(author.get("simpleText") or "").strip() or "anonymous"
    return {"t": round(t, 3), "user": user, "text": text}


def parse_youtube_live_chat_jsonl(lines: Iterable[str]) -> list[dict]:
    """Parse yt-dlp's ``*.live_chat.json`` (JSONL) into the app's chat format.

    Skips blank lines and entries that aren't plain text messages. Sorts by
    timestamp so the output is monotonically increasing even if the source
    isn't (it usually is).
    """
    out: list[dict] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg = _youtube_entry_to_message(entry)
        if msg is not None:
            out.append(msg)
    out.sort(key=lambda m: m["t"])
    return out


def parse_twitch_chat_json(raw: object) -> list[dict]:
    """Parse chat-downloader's JSON output into the app's chat format.

    chat-downloader writes a JSON array of message dicts. We pull
    ``time_in_seconds`` (already video-relative), ``author.name``, and
    ``message`` (plain text). Non-text events (subs, raids, system messages
    with no ``time_in_seconds``) are skipped.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        t = entry.get("time_in_seconds")
        if t is None:
            continue
        try:
            t = float(t)
        except (TypeError, ValueError):
            continue
        text = entry.get("message")
        if not isinstance(text, str) or not text.strip():
            continue
        author = entry.get("author") or {}
        user = ""
        if isinstance(author, dict):
            user = str(
                author.get("display_name")
                or author.get("name")
                or ""
            ).strip()
        out.append({"t": round(t, 3), "user": user or "anonymous", "text": text.strip()})
    out.sort(key=lambda m: m["t"])
    return out


# ---------------------------------------------------------------------------
# Subprocess wrappers
# ---------------------------------------------------------------------------


class FetchError(Exception):
    """Raised when an external download/extract step fails. Carries an exit code."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _require_command(name: str, install_hint: str, exit_code: int) -> str:
    path = shutil.which(name)
    if path is None:
        raise FetchError(
            f"required command not found on PATH: {name!r} ({install_hint})",
            exit_code=exit_code,
        )
    return path


def download_video(url: str, output_path: Path) -> None:
    """Download the source video to ``output_path`` (.mp4) via yt-dlp.

    Uses a ``%(ext)s`` template so yt-dlp picks the right extension before
    muxing, then we verify the final file landed where we expect.
    """
    yt_dlp = _require_command("yt-dlp", "pip install yt-dlp", exit_code=21)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    stem = output_path.with_suffix("")
    cmd = [
        yt_dlp,
        "--no-playlist",
        "--merge-output-format", "mp4",
        "-f", "bv*+ba/b",
        "-o", f"{stem}.%(ext)s",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FetchError(
            f"yt-dlp failed (exit {proc.returncode}):\n{proc.stderr.strip()}",
            exit_code=22,
        )
    if not output_path.exists():
        raise FetchError(
            f"yt-dlp finished but expected output not found: {output_path}",
            exit_code=22,
        )


def download_youtube_chat(url: str, output_stem: Path) -> Path:
    """Download a YouTube live-chat replay JSONL via yt-dlp.

    yt-dlp writes the subtitle to ``<stem>.live_chat.json``. Raises
    ``FetchError(25)`` if the file isn't produced (typical for non-live
    videos that have no chat replay).
    """
    yt_dlp = _require_command("yt-dlp", "pip install yt-dlp", exit_code=21)
    output_stem.parent.mkdir(parents=True, exist_ok=True)
    stem = output_stem.with_suffix("")
    cmd = [
        yt_dlp,
        "--no-playlist",
        "--skip-download",
        "--write-subs",
        "--sub-langs", "live_chat",
        "-o", f"{stem}.%(ext)s",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FetchError(
            f"yt-dlp (chat) failed (exit {proc.returncode}):\n{proc.stderr.strip()}",
            exit_code=22,
        )
    chat_path = Path(f"{stem}.live_chat.json")
    if not chat_path.exists():
        raise FetchError(
            f"yt-dlp produced no live_chat.json at {chat_path} "
            "(video may not be a live-stream archive with chat replay)",
            exit_code=25,
        )
    return chat_path


def download_twitch_chat(url: str, output_path: Path) -> Path:
    """Download a Twitch VOD chat to ``output_path`` (raw chat-downloader JSON)."""
    cd = _require_command(
        "chat-downloader",
        "pip install chat-downloader",
        exit_code=23,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [cd, url, "--output", str(output_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FetchError(
            f"chat-downloader failed (exit {proc.returncode}):\n{proc.stderr.strip()}",
            exit_code=24,
        )
    if not output_path.exists():
        raise FetchError(
            f"chat-downloader produced no output at {output_path}",
            exit_code=24,
        )
    return output_path


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _derive_name(url: str) -> str:
    """Fallback ``--name`` derivation: use the YouTube ``v=`` id or the last path segment."""
    m = re.search(r"[?&]v=([A-Za-z0-9_-]+)", url)
    if m:
        return m.group(1)
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9_-]", "_", tail) or "video"


def fetch(url: str, output_dir: Path, name: str | None = None,
          *, skip_chat: bool = False) -> tuple[Path, Path | None]:
    """Download video + chat for ``url`` into ``output_dir``.

    Returns ``(video_path, chat_path)`` where ``chat_path`` is ``None`` only
    when ``skip_chat=True``. On chat failures, the video is kept but
    ``FetchError`` is re-raised so the caller can decide.
    """
    platform = detect_platform(url)
    name = name or _derive_name(url)
    output_dir.mkdir(parents=True, exist_ok=True)
    video_path = output_dir / f"{name}.mp4"
    chat_path = output_dir / f"{name}.chat.json"

    print(f"[fetch] platform={platform} name={name}", file=sys.stderr)
    print(f"[fetch] downloading video → {video_path}", file=sys.stderr)
    download_video(url, video_path)

    if skip_chat:
        return video_path, None

    if platform == "youtube":
        chat_stem = output_dir / name
        print(f"[fetch] downloading YouTube live chat replay", file=sys.stderr)
        raw_chat = download_youtube_chat(url, chat_stem)
        try:
            with raw_chat.open("r", encoding="utf-8") as f:
                messages = parse_youtube_live_chat_jsonl(f)
        except OSError as e:
            raise FetchError(f"could not read raw chat {raw_chat}: {e}", exit_code=26)
    else:  # twitch
        raw_chat = output_dir / f"{name}.chat.raw.json"
        print(f"[fetch] downloading Twitch VOD chat", file=sys.stderr)
        download_twitch_chat(url, raw_chat)
        try:
            raw = json.loads(raw_chat.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise FetchError(f"could not parse raw chat {raw_chat}: {e}", exit_code=26)
        messages = parse_twitch_chat_json(raw)

    chat_path.write_text(
        json.dumps(messages, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"[fetch] wrote {len(messages)} chat messages → {chat_path}",
        file=sys.stderr,
    )
    return video_path, chat_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download a video + chat from a URL into the app's expected format.",
    )
    p.add_argument("--url", required=True, help="YouTube or Twitch URL")
    p.add_argument("--output", type=Path, default=Path("samples"),
                   help="Output directory (default: samples/)")
    p.add_argument("--name", default=None,
                   help="Basename for output files (default: derived from URL)")
    p.add_argument("--skip-chat", action="store_true",
                   help="Download video only, skip chat extraction.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        video, chat = fetch(args.url, args.output, args.name, skip_chat=args.skip_chat)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 20
    except FetchError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return e.exit_code

    print(f"\nVideo: {video}")
    if chat is not None:
        print(f"Chat:  {chat}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
