"""Hotspot candidate detection.

Pluggable interface. Three implementations live here today:

- ``EvenSamplingDetector`` — deterministic placeholder, splits the video into
  N evenly-spaced windows. Useful as a fallback and for testing.
- ``AudioRmsDetector`` — picks windows around audio loudness peaks. Extracts
  raw PCM via ffmpeg and computes per-second RMS in Python (see the design
  log in docs/tasks.md for why we don't parse ffmpeg ``astats`` text output).
- ``CommentDensityDetector`` — picks windows around bursts of live-chat
  activity. Counts unique users per bin so a single spammer can't dominate.

New detectors should implement ``HotspotDetector.detect()`` and register in
``build_detector()``.
"""
from __future__ import annotations

import array
import json
import math
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class HotspotCandidate:
    start: float
    end: float
    score: float
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


class HotspotDetector(ABC):
    @abstractmethod
    def detect(
        self,
        *,
        input_path: Path,
        duration: float,
        debug_dir: Path | None = None,
    ) -> list[HotspotCandidate]:
        ...


class EvenSamplingDetector(HotspotDetector):
    """Pick ``count`` windows of ``window_seconds``, evenly spaced across the video."""

    def __init__(self, count: int, window_seconds: float) -> None:
        self.count = max(1, count)
        self.window_seconds = max(1.0, window_seconds)

    def detect(
        self,
        *,
        input_path: Path,  # unused — kept for contract uniformity
        duration: float,
        debug_dir: Path | None = None,  # unused
    ) -> list[HotspotCandidate]:
        del input_path, debug_dir  # intentionally ignored
        if duration <= 0:
            return []

        if duration <= self.window_seconds or self.count == 1:
            return [
                HotspotCandidate(
                    start=0.0,
                    end=round(min(self.window_seconds, duration), 3),
                    score=0.5,
                    reason="temporary placeholder: single segment",
                )
            ]

        usable = duration - self.window_seconds
        step = usable / (self.count - 1)
        candidates: list[HotspotCandidate] = []
        for i in range(self.count):
            start = round(i * step, 3)
            end = round(min(start + self.window_seconds, duration), 3)
            candidates.append(
                HotspotCandidate(
                    start=start,
                    end=end,
                    score=0.5,
                    reason="temporary placeholder: evenly sampled segment",
                )
            )
        return candidates


# ---------------------------------------------------------------------------
# Audio RMS detector
# ---------------------------------------------------------------------------

class AudioExtractionError(RuntimeError):
    """Raised when ffmpeg fails to extract audio for the audio_rms detector."""


class AudioRmsDetector(HotspotDetector):
    """Pick the loudest ``count`` non-overlapping windows.

    Algorithm:
      1. ffmpeg → mono ``SAMPLE_RATE`` Hz s16le PCM on stdout.
      2. Bin into ``BIN_SECONDS``-long windows, compute RMS per bin (in dBFS).
      3. Sort bins by RMS descending. Greedy NMS: pick a bin only if it is
         at least ``window_seconds`` away from any already-picked bin.
      4. For each pick, build a window of ``window_seconds`` centered at the
         bin start (clamped to [0, duration]).

    The score is min-max normalized RMS in [0, 1]. ``reason`` includes the
    raw dB so a human reviewer can sanity-check the pick.
    """

    SAMPLE_RATE = 4000   # mono — 4kHz is plenty for loudness, ~14M samples/h
    BIN_SECONDS = 1.0    # one RMS measurement per second

    def __init__(self, count: int, window_seconds: float) -> None:
        self.count = max(1, count)
        self.window_seconds = max(1.0, window_seconds)

    def detect(
        self,
        *,
        input_path: Path,
        duration: float,
        debug_dir: Path | None = None,
    ) -> list[HotspotCandidate]:
        del duration  # ffmpeg gives us the actual sample count; don't second-guess

        rms_series = self._extract_rms_series(input_path)
        if debug_dir is not None and rms_series:
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / "audio_rms.json").write_text(
                json.dumps(
                    [{"t": round(t, 3), "rms_db": round(db, 3) if math.isfinite(db) else None}
                     for t, db in rms_series],
                    indent=2,
                ),
                encoding="utf-8",
            )

        if not rms_series:
            return []

        finite_values = [db for _, db in rms_series if math.isfinite(db)]
        if not finite_values:
            return []  # all-silent input
        rms_min = min(finite_values)
        rms_max = max(finite_values)
        rms_range = (rms_max - rms_min) if rms_max > rms_min else 1.0

        def normalize(db: float) -> float:
            if not math.isfinite(db):
                return 0.0
            return max(0.0, min(1.0, (db - rms_min) / rms_range))

        # Greedy NMS over bins sorted by loudness.
        sorted_bins = sorted(rms_series, key=lambda x: x[1], reverse=True)
        picks: list[tuple[float, float]] = []
        for t, db in sorted_bins:
            if not math.isfinite(db):
                continue
            if len(picks) >= self.count:
                break
            if any(abs(t - pt) < self.window_seconds for pt, _ in picks):
                continue
            picks.append((t, db))

        # Build candidate windows around each pick, clamped to actual audio span.
        audio_end = rms_series[-1][0] + self.BIN_SECONDS
        candidates: list[HotspotCandidate] = []
        for t, db in picks:
            start = max(0.0, t - self.window_seconds / 2)
            end = min(audio_end, start + self.window_seconds)
            if end - start < self.window_seconds:
                start = max(0.0, end - self.window_seconds)
            candidates.append(
                HotspotCandidate(
                    start=round(start, 3),
                    end=round(end, 3),
                    score=round(normalize(db), 3),
                    reason=f"audio peak: {db:.1f} dBFS",
                )
            )

        candidates.sort(key=lambda c: c.start)
        return candidates

    def _extract_rms_series(self, input_path: Path) -> list[tuple[float, float]]:
        """Return ``[(t_seconds, rms_dbfs), ...]`` for each non-overlapping bin."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise AudioExtractionError(
                "ffmpeg not found on PATH. Install ffmpeg "
                "(macOS: `brew install ffmpeg`, Ubuntu: `apt install ffmpeg`) "
                "to use the audio_rms detector."
            )

        cmd = [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(input_path),
            "-vn",                              # drop video
            "-ac", "1",                          # mono
            "-ar", str(self.SAMPLE_RATE),        # downsample
            "-f", "s16le",                       # raw 16-bit little-endian PCM
            "-",
        ]
        try:
            result = subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
            # Treat any "no usable audio stream" variant as empty signal.
            no_audio_markers = (
                "Stream specifier",
                "does not match any streams",
                "Output file does not contain any stream",
                "Output file #0 does not contain any stream",
            )
            if any(m in stderr for m in no_audio_markers):
                return []
            raise AudioExtractionError(
                f"ffmpeg audio extraction failed: {stderr or 'no stderr'}"
            ) from exc

        pcm = result.stdout
        if not pcm:
            return []

        samples = array.array("h")
        samples.frombytes(pcm)

        bin_size = max(1, int(self.SAMPLE_RATE * self.BIN_SECONDS))
        n_bins = len(samples) // bin_size
        ref_amplitude = float(2 ** 15)  # full-scale for 16-bit signed

        series: list[tuple[float, float]] = []
        for b in range(n_bins):
            start_idx = b * bin_size
            end_idx = start_idx + bin_size
            sumsq = 0
            # Tight loop — kept simple; numpy not added on purpose.
            for i in range(start_idx, end_idx):
                v = samples[i]
                sumsq += v * v
            mean_sq = sumsq / bin_size
            if mean_sq <= 0:
                db = float("-inf")
            else:
                rms_linear = math.sqrt(mean_sq) / ref_amplitude
                db = 20.0 * math.log10(rms_linear) if rms_linear > 0 else float("-inf")
            t = (b * bin_size) / self.SAMPLE_RATE
            series.append((t, db))
        return series


# ---------------------------------------------------------------------------
# Comment density detector (live chat → hotspots)
# ---------------------------------------------------------------------------

class CommentDensityDetector(HotspotDetector):
    """Pick windows with the highest live-chat comment density.

    Input: a chat-log JSON file with timestamps relative to the video start::

        [
          {"t": 12.5, "user": "alice", "text": "lol"},
          {"t": 13.1, "user": "bob",   "text": "草"},
          ...
        ]

    Algorithm: bin messages by ``BIN_SECONDS``, count **unique users** per bin
    (so a single spammer can't dominate), greedy NMS top-K. ``score`` is the
    min-max-normalised unique-user count. ``reason`` includes the raw count.

    Source-format notes (see ``docs/workflow.md``):

    - Twitch chat replay → use ``--chat-format twitch_replay`` (TODO)
    - YouTube live chat (``yt-dlp --live-from-start --write-info-json``) →
      ``--chat-format youtube_yt_dlp`` (TODO)
    - For now only the canonical normalised JSON above is accepted.
    """

    BIN_SECONDS = 10.0  # 10s feels right for chat — tune later from real data

    def __init__(self, count: int, window_seconds: float, chat_log_path: Path) -> None:
        self.count = max(1, count)
        self.window_seconds = max(1.0, window_seconds)
        self.chat_log_path = chat_log_path

    def detect(
        self,
        *,
        input_path: Path,
        duration: float,
        debug_dir: Path | None = None,
    ) -> list[HotspotCandidate]:
        del input_path  # not needed — chat log is the signal

        if not self.chat_log_path.exists():
            raise FileNotFoundError(
                f"Chat log not found: {self.chat_log_path}. "
                "Pass --chat-log <path> to point at the chat history."
            )
        try:
            raw = json.loads(self.chat_log_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid chat-log JSON {self.chat_log_path}: {e}") from e

        if isinstance(raw, dict) and "messages" in raw:
            messages = raw["messages"]
        else:
            messages = raw

        n_bins = max(1, int(math.ceil(duration / self.BIN_SECONDS)))
        unique_users: list[set[str]] = [set() for _ in range(n_bins)]
        msg_count: list[int] = [0] * n_bins

        for m in messages:
            try:
                t = float(m.get("t", -1))
            except (TypeError, ValueError):
                continue
            if t < 0 or t >= duration:
                continue
            b = int(t / self.BIN_SECONDS)
            unique_users[b].add(str(m.get("user", "")))
            msg_count[b] += 1

        density = [len(s) for s in unique_users]

        if debug_dir is not None:
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / "comment_density.json").write_text(
                json.dumps(
                    [
                        {"t": round(i * self.BIN_SECONDS, 3),
                         "unique_users": density[i],
                         "messages": msg_count[i]}
                        for i in range(n_bins)
                    ],
                    indent=2,
                ),
                encoding="utf-8",
            )

        if not any(density):
            return []

        d_max = max(density)
        d_min = min(density)
        d_range = (d_max - d_min) if d_max > d_min else 1.0

        sorted_bins = sorted(
            ((i * self.BIN_SECONDS, density[i]) for i in range(n_bins) if density[i] > 0),
            key=lambda x: x[1],
            reverse=True,
        )
        picks: list[tuple[float, int]] = []
        for t, d in sorted_bins:
            if len(picks) >= self.count:
                break
            if any(abs(t - pt) < self.window_seconds for pt, _ in picks):
                continue
            picks.append((t, d))

        candidates: list[HotspotCandidate] = []
        for t, d in picks:
            start = max(0.0, t - self.window_seconds / 2)
            end = min(duration, start + self.window_seconds)
            if end - start < self.window_seconds:
                start = max(0.0, end - self.window_seconds)
            score = (d - d_min) / d_range
            candidates.append(
                HotspotCandidate(
                    start=round(start, 3),
                    end=round(end, 3),
                    score=round(max(0.0, min(1.0, score)), 3),
                    reason=f"comment density: {d} unique users in {self.BIN_SECONDS:g}s",
                )
            )

        candidates.sort(key=lambda c: c.start)
        return candidates


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

# Exposed so callers can discover what is available without importing the
# classes individually.
AVAILABLE_DETECTORS: tuple[str, ...] = (
    "even",
    "audio_rms",
    "comment_density",
)


def build_detector(
    name: str,
    count: int,
    window_seconds: float,
    *,
    chat_log_path: Path | None = None,
) -> HotspotDetector:
    """Factory keyed by detector name from the CLI.

    ``chat_log_path`` is required when ``name == 'comment_density'``.
    """
    if name == "even":
        return EvenSamplingDetector(count=count, window_seconds=window_seconds)
    if name == "audio_rms":
        return AudioRmsDetector(count=count, window_seconds=window_seconds)
    if name == "comment_density":
        if chat_log_path is None:
            raise ValueError(
                "comment_density detector requires --chat-log <path>."
            )
        return CommentDensityDetector(
            count=count,
            window_seconds=window_seconds,
            chat_log_path=chat_log_path,
        )
    raise ValueError(
        f"Unknown detector '{name}'. Available: {', '.join(AVAILABLE_DETECTORS)}. "
        "Add new detectors in src/hotspot_detector.py and register them here."
    )
