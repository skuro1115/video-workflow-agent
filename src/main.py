"""CLI entrypoint for the video-workflow MVP pipeline.

Two modes:

1. **Full pipeline** (default)
   ::

       python -m src.main --input samples/sample.mp4 --output output/

   Writes ``video_info.json``, ``hotspot_candidates.json``, ``clip_plan.json``
   into the output directory. Pass ``--export-clips`` to also encode each
   planned clip into ``<output>/clips/`` with ffmpeg.

2. **From-plan re-export**
   ::

       python -m src.main --input samples/sample.mp4 --output output/ \\
                          --from-plan output/clip_plan.json

   Skips probe + detection + planning; reads the plan JSON directly and runs
   only the export step. Useful when a human has reviewed and edited the plan
   before encoding.

Pass ``--debug`` to write detector intermediate artefacts (e.g. the raw RMS
series for ``audio_rms``) into ``<output>/debug/``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .clip_exporter import FFmpegNotFoundError, export_clips
from .clip_planner import ClipPlan, plan_clips
from .config import PipelineConfig
from .hotspot_detector import AudioExtractionError, build_detector
from .video_info import FFprobeFailedError, FFprobeNotFoundError, probe


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _load_plan(path: Path) -> list[ClipPlan]:
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    plans: list[ClipPlan] = []
    for entry in raw:
        plans.append(ClipPlan(
            clip_id=entry["clip_id"],
            source_start=float(entry["source_start"]),
            source_end=float(entry["source_end"]),
            duration=float(entry["duration"]),
            purpose=entry.get("purpose", "short clip candidate"),
            status=entry.get("status", "planned"),
            score=float(entry.get("score", 0.5)),
            reason=entry.get("reason", ""),
        ))
    return plans


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Long-form video → hotspot → clip pipeline (MVP)",
    )
    p.add_argument("--input", required=True, type=Path, help="Path to input video")
    p.add_argument("--output", required=True, type=Path, help="Output directory")
    p.add_argument(
        "--detector", default="even",
        help="Hotspot detector name. Available: 'even', 'audio_rms'.",
    )
    p.add_argument("--candidates", type=int, default=6, help="Number of hotspot candidates")
    p.add_argument(
        "--window", type=float, default=30.0,
        help="Candidate window length in seconds",
    )
    p.add_argument("--min-duration", type=float, default=10.0)
    p.add_argument("--max-duration", type=float, default=60.0)
    p.add_argument(
        "--export-clips", action="store_true",
        help="Cut clips with ffmpeg (default: only write the JSON plan).",
    )
    p.add_argument(
        "--from-plan", type=Path, default=None,
        help="Skip detection; load this clip_plan.json and run only export. "
             "Implies --export-clips.",
    )
    p.add_argument(
        "--debug", action="store_true",
        help="Write detector intermediate artefacts to <output>/debug/.",
    )
    return p.parse_args(argv)


def _run_full_pipeline(cfg: PipelineConfig, *, debug: bool) -> int:
    print(f"[1/4] Probing video: {cfg.input_path}")
    try:
        info = probe(cfg.input_path)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except FFprobeNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3
    except FFprobeFailedError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 4

    _write_json(cfg.output_dir / "video_info.json", info.to_dict())
    print(
        f"      duration={info.duration:.2f}s  "
        f"{info.width}x{info.height}  fps={info.fps}  codec={info.video_codec}"
    )

    print(f"[2/4] Detecting hotspot candidates (detector={cfg.detector})")
    detector = build_detector(cfg.detector, cfg.candidate_count, cfg.candidate_duration)
    debug_dir = (cfg.output_dir / "debug") if debug else None
    try:
        candidates = detector.detect(
            input_path=cfg.input_path,
            duration=info.duration,
            debug_dir=debug_dir,
        )
    except AudioExtractionError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 6
    _write_json(
        cfg.output_dir / "hotspot_candidates.json",
        [c.to_dict() for c in candidates],
    )
    print(f"      {len(candidates)} candidates")

    print("[3/4] Planning clips")
    plans = plan_clips(
        candidates,
        min_duration=cfg.min_clip_duration,
        max_duration=cfg.max_clip_duration,
    )
    _write_json(
        cfg.output_dir / "clip_plan.json",
        [p.to_dict() for p in plans],
    )
    print(f"      {len(plans)} clips planned")

    if cfg.export_clips:
        return _run_export(cfg, plans)
    print("[4/4] Skipped export (pass --export-clips to cut actual clips)")
    print(f"Done. Output: {cfg.output_dir}")
    return 0


def _run_export(cfg: PipelineConfig, plans: list[ClipPlan]) -> int:
    print("[4/4] Exporting clips with ffmpeg")
    try:
        results = export_clips(
            input_path=cfg.input_path,
            output_dir=cfg.output_dir / "clips",
            plans=plans,
            video_codec=cfg.video_codec,
            audio_codec=cfg.audio_codec,
        )
    except FFmpegNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 5
    _write_json(cfg.output_dir / "clip_export_result.json", results)
    ok = sum(1 for r in results if r["status"] == "exported")
    print(f"      exported {ok}/{len(results)}")
    print(f"Done. Output: {cfg.output_dir}")
    return 0


def _run_from_plan(cfg: PipelineConfig, plan_path: Path) -> int:
    print(f"[from-plan] Loading: {plan_path}")
    if not plan_path.exists():
        print(f"ERROR: plan file not found: {plan_path}", file=sys.stderr)
        return 7
    if not cfg.input_path.exists():
        print(f"ERROR: Input video not found: {cfg.input_path}", file=sys.stderr)
        return 2
    try:
        plans = _load_plan(plan_path)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        print(f"ERROR: failed to parse plan {plan_path}: {e}", file=sys.stderr)
        return 8
    print(f"            {len(plans)} clips loaded")
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    return _run_export(cfg, plans)


def run(cfg: PipelineConfig, *, from_plan: Path | None, debug: bool) -> int:
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    if from_plan is not None:
        return _run_from_plan(cfg, from_plan)
    return _run_full_pipeline(cfg, debug=debug)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = PipelineConfig(
        input_path=args.input,
        output_dir=args.output,
        detector=args.detector,
        candidate_count=args.candidates,
        candidate_duration=args.window,
        min_clip_duration=args.min_duration,
        max_clip_duration=args.max_duration,
        export_clips=args.export_clips or args.from_plan is not None,
    )
    return run(cfg, from_plan=args.from_plan, debug=args.debug)


if __name__ == "__main__":
    raise SystemExit(main())
