"""Convert hotspot candidates into a concrete clip plan.

A clip plan is the contract between detection and export: every entry is
self-describing (id, source range, why it was picked) so downstream tools
(SNS upload, captioning, manual review UI) can consume it without re-running
detection.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .hotspot_detector import HotspotCandidate


@dataclass
class ClipPlan:
    clip_id: str
    source_start: float
    source_end: float
    duration: float
    purpose: str
    status: str
    score: float
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def plan_clips(
    candidates: list[HotspotCandidate],
    *,
    min_duration: float,
    max_duration: float,
    purpose: str = "short clip candidate",
) -> list[ClipPlan]:
    """Trim each candidate to [min_duration, max_duration] and assign a clip_id.

    Candidates shorter than `min_duration` are dropped. Candidates longer than
    `max_duration` are truncated from the end.
    """
    plans: list[ClipPlan] = []
    next_index = 1
    for cand in candidates:
        raw_duration = max(0.0, cand.end - cand.start)
        if raw_duration < min_duration:
            continue
        clipped_duration = min(raw_duration, max_duration)
        end = cand.start + clipped_duration
        plans.append(
            ClipPlan(
                clip_id=f"clip_{next_index:03d}",
                source_start=round(cand.start, 3),
                source_end=round(end, 3),
                duration=round(clipped_duration, 3),
                purpose=purpose,
                status="planned",
                score=cand.score,
                reason=cand.reason,
            )
        )
        next_index += 1
    return plans
