"""Evaluate hotspot_candidates.json against a hand-curated expected.json.

The point of this script is to make detector tuning measurable: instead of
"the picks look reasonable", you get a hit rate, a list of misses, and a
machine-readable result file that AI-assisted sessions can compare across
runs.

Usage::

    python -m scripts.eval \\
        --hotspots output/hotspot_candidates.json \\
        --expected samples/varying.expected.json

    python -m scripts.eval ... --out output/eval_result.json   # also write JSON

Expected.json schema::

    {
      "video": "samples/varying.mp4",
      "duration": 120.0,
      "peaks": [
        # Either a t_range (recommended for events with known duration)
        {"t_range": [15.0, 20.0], "label": "loud sine burst"},
        # ...or a single t (with optional per-peak tolerance, default 5s)
        {"t": 60.0, "label": "scene change", "tolerance": 3.0}
      ]
    }

A peak is **hit** if at least one candidate's [start, end] overlaps the peak's
range (or, for single-t peaks, [t-tolerance, t+tolerance]). Hit rate is
hits / total_peaks. Precision is candidates_overlapping_any_peak / total
candidates — a candidate that doesn't line up with any expected peak counts
against precision.

Exit codes:
  0 — eval ran (regardless of hit rate)
  2 — input file missing
  3 — input JSON malformed
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TOLERANCE = 5.0  # seconds, applied when peak is given as single t


@dataclass
class Peak:
    start: float
    end: float
    label: str

    @classmethod
    def from_dict(cls, d: dict) -> "Peak":
        label = str(d.get("label", ""))
        if "t_range" in d:
            tr = d["t_range"]
            if not isinstance(tr, list) or len(tr) != 2:
                raise ValueError(f"peak t_range must be [start, end], got {tr!r}")
            start = float(tr[0])
            end = float(tr[1])
        elif "t" in d:
            t = float(d["t"])
            tol = float(d.get("tolerance", DEFAULT_TOLERANCE))
            start = t - tol
            end = t + tol
        else:
            raise ValueError(f"peak must have 't' or 't_range', got keys: {list(d)}")
        if end <= start:
            raise ValueError(f"peak end must be > start: {start}..{end}")
        return cls(start=start, end=end, label=label)


@dataclass
class Candidate:
    start: float
    end: float
    score: float
    reason: str


@dataclass
class EvalResult:
    total_peaks: int
    total_candidates: int
    hit_peaks: int
    hits_per_peak: list[dict] = field(default_factory=list)
    misses: list[dict] = field(default_factory=list)
    candidates_overlapping: int = 0

    @property
    def hit_rate(self) -> float:
        return self.hit_peaks / self.total_peaks if self.total_peaks else 0.0

    @property
    def precision(self) -> float:
        return (
            self.candidates_overlapping / self.total_candidates
            if self.total_candidates else 0.0
        )

    def to_dict(self) -> dict:
        return {
            "total_peaks": self.total_peaks,
            "total_candidates": self.total_candidates,
            "hit_peaks": self.hit_peaks,
            "hit_rate": round(self.hit_rate, 4),
            "candidates_overlapping": self.candidates_overlapping,
            "precision": round(self.precision, 4),
            "hits_per_peak": self.hits_per_peak,
            "misses": self.misses,
        }


def _overlaps(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    return a_start < b_end and b_start < a_end


def evaluate(peaks: list[Peak], candidates: list[Candidate]) -> EvalResult:
    """Compute hit rate, precision, and per-peak details."""
    result = EvalResult(
        total_peaks=len(peaks),
        total_candidates=len(candidates),
        hit_peaks=0,
    )

    for peak in peaks:
        matching = [
            i for i, c in enumerate(candidates)
            if _overlaps(peak.start, peak.end, c.start, c.end)
        ]
        if matching:
            result.hit_peaks += 1
            result.hits_per_peak.append({
                "label": peak.label,
                "peak_range": [round(peak.start, 3), round(peak.end, 3)],
                "matching_candidates": [
                    {
                        "index": i,
                        "start": round(candidates[i].start, 3),
                        "end": round(candidates[i].end, 3),
                        "score": candidates[i].score,
                    }
                    for i in matching
                ],
            })
        else:
            result.misses.append({
                "label": peak.label,
                "peak_range": [round(peak.start, 3), round(peak.end, 3)],
            })

    overlap_set: set[int] = set()
    for peak in peaks:
        for i, c in enumerate(candidates):
            if _overlaps(peak.start, peak.end, c.start, c.end):
                overlap_set.add(i)
    result.candidates_overlapping = len(overlap_set)

    return result


def _load_json(path: Path) -> object:
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(2)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON in {path}: {e}", file=sys.stderr)
        sys.exit(3)


def load_peaks(path: Path) -> list[Peak]:
    raw = _load_json(path)
    if isinstance(raw, dict) and "peaks" in raw:
        peaks_raw = raw["peaks"]
    elif isinstance(raw, list):
        peaks_raw = raw  # bare list also accepted
    else:
        print(
            f"ERROR: expected.json must be an object with 'peaks' or a list, "
            f"got {type(raw).__name__}",
            file=sys.stderr,
        )
        sys.exit(3)

    peaks: list[Peak] = []
    for i, entry in enumerate(peaks_raw):
        try:
            peaks.append(Peak.from_dict(entry))
        except (ValueError, KeyError, TypeError) as e:
            print(f"ERROR: peak[{i}] invalid: {e}", file=sys.stderr)
            sys.exit(3)
    return peaks


def load_candidates(path: Path) -> list[Candidate]:
    raw = _load_json(path)
    if not isinstance(raw, list):
        print(
            f"ERROR: hotspot_candidates.json must be a list, "
            f"got {type(raw).__name__}",
            file=sys.stderr,
        )
        sys.exit(3)
    return [
        Candidate(
            start=float(c["start"]),
            end=float(c["end"]),
            score=float(c.get("score", 0.0)),
            reason=str(c.get("reason", "")),
        )
        for c in raw
    ]


def format_summary(result: EvalResult) -> str:
    lines = [
        f"Peaks: {result.hit_peaks}/{result.total_peaks} hit "
        f"({result.hit_rate * 100:.1f}%)",
        f"Candidates: {result.candidates_overlapping}/{result.total_candidates} overlap "
        f"({result.precision * 100:.1f}% precision)",
    ]
    if result.misses:
        lines.append("")
        lines.append("Misses:")
        for m in result.misses:
            lines.append(f"  - {m['label']}  range={m['peak_range']}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Evaluate detector output against a hand-curated expected.json.",
    )
    p.add_argument("--hotspots", type=Path, required=True,
                   help="Path to hotspot_candidates.json from a pipeline run.")
    p.add_argument("--expected", type=Path, required=True,
                   help="Path to expected.json (ground-truth peaks).")
    p.add_argument("--out", type=Path, default=None,
                   help="Optional path to write the full eval result as JSON.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    peaks = load_peaks(args.expected)
    candidates = load_candidates(args.hotspots)
    result = evaluate(peaks, candidates)

    print(format_summary(result))

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"\nWrote {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
