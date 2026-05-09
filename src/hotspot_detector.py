"""Hotspot candidate detection.

Pluggable interface. Four implementations live here today:

- ``EvenSamplingDetector`` — deterministic placeholder, splits the video into
  N evenly-spaced windows. Useful as a fallback and for testing.
- ``AudioRmsDetector`` — picks windows around audio loudness peaks. Extracts
  raw PCM via ffmpeg and computes per-second RMS in Python (see the design
  log in docs/tasks.md for why we don't parse ffmpeg ``astats`` text output).
- ``CommentDensityDetector`` — picks windows around bursts of live-chat
  activity. Counts unique users per bin so a single spammer can't dominate.
- ``CompositeDetector`` — runs multiple sub-detectors and combines their
  per-bin scores via a weighted sum (weights configured externally; see
  ``score_weights.py``).

New detectors should implement ``HotspotDetector.detect()`` and register in
``build_detector()``.
"""
from __future__ import annotations

import array
import json
import math
import shutil
import subprocess
import sys
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
                "Pass --chat-log <path> or remove comment_density from --weights."
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
# Composite detector — weighted combination of multiple sub-detectors
# ---------------------------------------------------------------------------

@dataclass
class SubDetectorSpec:
    name: str
    detector: HotspotDetector
    weight: float


class CompositeDetector(HotspotDetector):
    """Run multiple sub-detectors and combine their scores into a single ranking.

    Two fusion modes are available:

    * ``weighted_sum`` (default) — min-max normalise each detector's scores to
      [0, 1] within this video, project each candidate's normalised score onto
      a per-bin score array, and take ``sum(w_i * normalised_i) / sum(w_i)``.
      Intuitive but easily skewed by an outlier candidate.

    * ``rrf`` (Reciprocal Rank Fusion) — for each detector, rank its candidates
      by score descending (rank 1 = best). Each bin a candidate covers inherits
      that detector's rank. Bin score is
      ``sum_i (w_i / (rrf_k + rank_i)) / max_possible``.
      RRF discards score magnitudes and uses only the relative ordering, so
      one outlier candidate can't drown out the others.

    Common post-processing for both modes:

    1. Greedy NMS top-K over bins, picks separated by ``window_seconds``
    2. Apply ``min_score`` threshold
    3. Reason strings cite contributing detectors so reviewers can see why
       each clip was picked. ``weighted_sum`` shows weighted scores;
       ``rrf`` shows ranks.
    """

    def __init__(
        self,
        sub_detectors: list[SubDetectorSpec],
        *,
        count: int,
        window_seconds: float,
        bin_seconds: float = 1.0,
        min_score: float = 0.0,
        fusion: str = "weighted_sum",
        rrf_k: int = 60,
    ) -> None:
        if fusion not in ("weighted_sum", "rrf"):
            raise ValueError(
                f"unknown fusion mode {fusion!r}; expected 'weighted_sum' or 'rrf'"
            )
        self.sub_detectors = sub_detectors
        self.count = max(1, count)
        self.window_seconds = max(1.0, window_seconds)
        self.bin_seconds = max(0.1, bin_seconds)
        self.min_score = max(0.0, min_score)
        self.fusion = fusion
        self.rrf_k = max(1, rrf_k)

    def detect(
        self,
        *,
        input_path: Path,
        duration: float,
        debug_dir: Path | None = None,
    ) -> list[HotspotCandidate]:
        if duration <= 0:
            return []

        active = [s for s in self.sub_detectors if s.weight > 0]
        if not active:
            return []

        n_bins = max(1, int(math.ceil(duration / self.bin_seconds)))
        combined = [0.0] * n_bins
        # Track per-bin contributing detectors so reasons can cite them.
        # For weighted_sum: value is weight*norm. For rrf: value is rank (int).
        contributors: list[dict[str, float]] = [dict() for _ in range(n_bins)]

        per_detector_dump: dict[str, list[dict]] = {}

        # First pass: run all detectors, collecting candidates per active spec.
        # Keep them around so the second pass can do whichever fusion is wired.
        per_detector_cands: list[tuple[SubDetectorSpec, list[HotspotCandidate]]] = []
        for spec in active:
            try:
                cands = spec.detector.detect(
                    input_path=input_path,
                    duration=duration,
                    debug_dir=debug_dir,
                )
            except Exception as e:  # detector-level fault tolerance
                print(
                    f"WARNING: sub-detector '{spec.name}' failed: {e}",
                    file=sys.stderr,
                )
                continue
            per_detector_dump[spec.name] = [c.to_dict() for c in cands]
            if cands:
                per_detector_cands.append((spec, cands))

        if self.fusion == "rrf":
            # RRF: rank candidates per detector, project ranks onto bins,
            # then combine via sum_i (w_i / (k + rank_i)) and normalise so
            # the theoretical maximum (every detector ranks bin #1) -> 1.0.
            best_inv = 1.0 / (self.rrf_k + 1)
            max_possible = sum(spec.weight for spec, _ in per_detector_cands) * best_inv
            for spec, cands in per_detector_cands:
                # Rank by score desc; ties get the same dense rank so neither
                # candidate drops out.
                sorted_cands = sorted(cands, key=lambda c: c.score, reverse=True)
                # Per-bin best (lowest) rank from this detector — a bin that
                # gets covered by multiple candidates from the same detector
                # should take the best one.
                best_rank: dict[int, int] = {}
                for rank0, c in enumerate(sorted_cands):
                    rank = rank0 + 1
                    start_bin = max(0, int(c.start / self.bin_seconds))
                    end_bin = min(n_bins, int(math.ceil(c.end / self.bin_seconds)))
                    for b in range(start_bin, end_bin):
                        prev = best_rank.get(b)
                        if prev is None or rank < prev:
                            best_rank[b] = rank
                for b, rank in best_rank.items():
                    contribution = spec.weight / (self.rrf_k + rank)
                    combined[b] += contribution
                    # Store rank (so reason can show "audio_rms@rank=2").
                    contributors[b][spec.name] = float(rank)
            if max_possible > 0:
                for i in range(n_bins):
                    combined[i] /= max_possible
        else:
            # weighted_sum: project min-max-normalised scores onto bins.
            total_weight = sum(spec.weight for spec, _ in per_detector_cands)
            for spec, cands in per_detector_cands:
                scores = [c.score for c in cands]
                s_min = min(scores)
                s_max = max(scores)
                # Edge case: if every candidate from this detector has the same
                # score (including the single-candidate case), min-max would
                # produce 0 for all of them and the detector would contribute
                # nothing. Treat them all as equally maximal instead.
                equal_scores = s_max <= s_min
                s_range = 1.0 if equal_scores else (s_max - s_min)
                for c in cands:
                    norm = 1.0 if equal_scores else (c.score - s_min) / s_range
                    start_bin = max(0, int(c.start / self.bin_seconds))
                    end_bin = min(n_bins, int(math.ceil(c.end / self.bin_seconds)))
                    for b in range(start_bin, end_bin):
                        combined[b] += spec.weight * norm
                        contributors[b][spec.name] = (
                            contributors[b].get(spec.name, 0.0) + spec.weight * norm
                        )
            # Normalise by total weight so combined ∈ [0, 1].
            if total_weight > 0:
                for i in range(n_bins):
                    combined[i] /= total_weight

        if debug_dir is not None:
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / "composite_combined.json").write_text(
                json.dumps(
                    {
                        "fusion": self.fusion,
                        "rrf_k": self.rrf_k if self.fusion == "rrf" else None,
                        "bins": [
                            {"t": round(i * self.bin_seconds, 3),
                             "score": round(combined[i], 4)}
                            for i in range(n_bins)
                        ],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            (debug_dir / "composite_subdetectors.json").write_text(
                json.dumps(per_detector_dump, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        # Strictly > 0: a bin nobody contributed to is not a candidate, even
        # when min_score is at its 0 default.
        sorted_bins = sorted(
            ((i, combined[i]) for i in range(n_bins)
             if combined[i] > 0 and combined[i] >= self.min_score),
            key=lambda x: x[1],
            reverse=True,
        )

        picks: list[tuple[int, float]] = []
        for bin_idx, score in sorted_bins:
            if len(picks) >= self.count:
                break
            t = bin_idx * self.bin_seconds
            if any(abs(t - pi * self.bin_seconds) < self.window_seconds for pi, _ in picks):
                continue
            picks.append((bin_idx, score))

        candidates: list[HotspotCandidate] = []
        for bin_idx, score in picks:
            t = bin_idx * self.bin_seconds
            start = max(0.0, t - self.window_seconds / 2)
            end = min(duration, start + self.window_seconds)
            if end - start < self.window_seconds:
                start = max(0.0, end - self.window_seconds)
            contrib = contributors[bin_idx]
            if contrib:
                if self.fusion == "rrf":
                    # contrib values are ranks (lower is better) — sort ascending
                    top = sorted(contrib.items(), key=lambda x: x[1])
                    reason = "composite (rrf): " + ", ".join(
                        f"{name}@rank={int(val)}" for name, val in top
                    )
                else:
                    top = sorted(contrib.items(), key=lambda x: x[1], reverse=True)
                    reason = "composite: " + ", ".join(
                        f"{name}={val:.2f}" for name, val in top
                    )
            else:
                reason = "composite (no contributing detectors at this bin)"
            candidates.append(
                HotspotCandidate(
                    start=round(start, 3),
                    end=round(end, 3),
                    score=round(score, 3),
                    reason=reason,
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
    "composite",
)


def build_detector(
    name: str,
    count: int,
    window_seconds: float,
    *,
    chat_log_path: Path | None = None,
    weights=None,  # src.score_weights.Weights — typed loosely to avoid import cycle
) -> HotspotDetector:
    """Factory keyed by detector name from the CLI.

    ``chat_log_path`` is required when ``name == 'comment_density'`` (or when
    ``composite`` includes ``comment_density``). ``weights`` is required when
    ``name == 'composite'``.
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
    if name == "composite":
        if weights is None or not weights.detectors:
            raise ValueError(
                "composite detector requires --weights <path> or "
                "--interactive-weights."
            )
        sub_specs: list[SubDetectorSpec] = []
        for dw in weights.enabled():
            if dw.name == "composite":
                continue  # nested composites not supported
            try:
                sub = build_detector(
                    dw.name,
                    count=count,
                    window_seconds=window_seconds,
                    chat_log_path=chat_log_path,
                )
            except ValueError as e:
                print(
                    f"WARNING: weight references detector '{dw.name}' but "
                    f"build failed ({e}); skipping.",
                    file=sys.stderr,
                )
                continue
            sub_specs.append(SubDetectorSpec(name=dw.name, detector=sub, weight=dw.weight))
        if not sub_specs:
            raise ValueError(
                "composite detector has no usable sub-detectors after filtering."
            )
        return CompositeDetector(
            sub_detectors=sub_specs,
            count=count,
            window_seconds=window_seconds,
            bin_seconds=weights.bin_seconds,
            min_score=weights.min_score,
            fusion=weights.fusion,
            rrf_k=weights.rrf_k,
        )
    raise ValueError(
        f"Unknown detector '{name}'. Available: {', '.join(AVAILABLE_DETECTORS)}. "
        "Add new detectors in src/hotspot_detector.py and register them here."
    )
