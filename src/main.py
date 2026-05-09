"""CLI entrypoint for the video-workflow MVP pipeline.

Modes
-----

1. **Full pipeline** (default)
   ::

       python -m src.main --input samples/sample.mp4 --output output/

   Writes ``video_info.json``, ``hotspot_candidates.json``, ``clip_plan.json``
   into the output directory. Pass ``--export-clips`` to also encode each
   planned clip into ``<output>/clips/`` with ffmpeg.

2. **From-plan re-export**
   ::

       python -m src.main --input video.mp4 --output output/ \\
                          --from-plan output/clip_plan.json

   Skips probe + detection + planning; reads the plan JSON directly and runs
   only the export step. Useful when a human has reviewed and edited the plan
   before encoding.

3. **List detectors**
   ::

       python -m src.main --list-detectors

   Prints the registered detector names and exits.

Detector selection
------------------

``--detector`` accepts:

- ``even``            placeholder (evenly-spaced windows)
- ``audio_rms``       audio loudness peaks (requires ffmpeg)
- ``comment_density`` live-chat density peaks (requires ``--chat-log <path>``)
- ``composite``       weighted combination of multiple sub-detectors. Requires
                      either ``--weights <path>`` or ``--interactive-weights``.

Pass ``--debug`` to write detector intermediate artefacts (raw RMS series,
combined per-bin score, comment density bins) into ``<output>/debug/``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .clip_exporter import FFmpegNotFoundError, export_clips
from .clip_planner import ClipPlan, plan_clips
from .config import PipelineConfig
from .hotspot_detector import AVAILABLE_DETECTORS, AudioExtractionError, build_detector
from .run_timer import RunTimer
from .score_weights import (
    Weights,
    WeightsConfigError,
    default_weights,
    interactive_weights,
    load_weights,
    maybe_save_interactive,
)
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
    p.add_argument(
        "--list-detectors", action="store_true",
        help="Print registered detector names and exit.",
    )
    p.add_argument("--input", type=Path, help="Path to input video")
    p.add_argument("--output", type=Path, help="Output directory")
    p.add_argument(
        "--detector", default="even",
        help="Hotspot detector name. Available: " + ", ".join(AVAILABLE_DETECTORS),
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
    p.add_argument(
        "--chat-log", type=Path, default=None,
        help="Path to a chat-log JSON for the comment_density detector.",
    )
    p.add_argument(
        "--weights", type=Path, default=None,
        help="Path to a JSON weights file for the composite detector.",
    )
    p.add_argument(
        "--interactive-weights", action="store_true",
        help="Prompt on stdin for composite-detector weights "
             "(implies --detector composite). Optionally saves to a file.",
    )
    return p.parse_args(argv)


def _resolve_weights(args: argparse.Namespace) -> Weights | None:
    """Decide which Weights object (if any) the run should use."""
    if args.weights is not None:
        try:
            return load_weights(args.weights)
        except WeightsConfigError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(9)
    if args.interactive_weights:
        defaults = default_weights()
        # Restrict prompt to detectors that actually exist (excluding 'composite' itself).
        available = [n for n in AVAILABLE_DETECTORS if n != "composite"]
        weights = interactive_weights(available, defaults=defaults)
        # Best-effort save; ignore IO errors so the pipeline still proceeds.
        try:
            maybe_save_interactive(weights)
        except OSError as e:
            print(f"WARNING: could not save weights: {e}", file=sys.stderr)
        return weights
    return None


def _run_full_pipeline(
    cfg: PipelineConfig,
    *,
    debug: bool,
    chat_log_path: Path | None,
    weights: Weights | None,
    timer: RunTimer,
) -> int:
    print(f"[1/4] Probing video: {cfg.input_path}")
    try:
        with timer.stage("probe"):
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
    try:
        detector = build_detector(
            cfg.detector,
            cfg.candidate_count,
            cfg.candidate_duration,
            chat_log_path=chat_log_path,
            weights=weights,
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 9
    debug_dir = (cfg.output_dir / "debug") if debug else None
    try:
        with timer.stage("detect", detector=cfg.detector):
            candidates = detector.detect(
                input_path=cfg.input_path,
                duration=info.duration,
                debug_dir=debug_dir,
            )
    except AudioExtractionError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 6
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 10
    _write_json(
        cfg.output_dir / "hotspot_candidates.json",
        [c.to_dict() for c in candidates],
    )
    print(f"      {len(candidates)} candidates")

    print("[3/4] Planning clips")
    with timer.stage("plan"):
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
        return _run_export(cfg, plans, timer=timer)
    print("[4/4] Skipped export (pass --export-clips to cut actual clips)")
    print(f"Done. Output: {cfg.output_dir}")
    return 0


def _run_export(cfg: PipelineConfig, plans: list[ClipPlan], *, timer: RunTimer) -> int:
    print("[4/4] Exporting clips with ffmpeg")
    try:
        with timer.stage("export", clips=len(plans)):
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


def _run_from_plan(cfg: PipelineConfig, plan_path: Path, *, timer: RunTimer) -> int:
    print(f"[from-plan] Loading: {plan_path}")
    if not plan_path.exists():
        print(f"ERROR: plan file not found: {plan_path}", file=sys.stderr)
        return 7
    if not cfg.input_path.exists():
        print(f"ERROR: Input video not found: {cfg.input_path}", file=sys.stderr)
        return 2
    try:
        with timer.stage("load_plan"):
            plans = _load_plan(plan_path)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        print(f"ERROR: failed to parse plan {plan_path}: {e}", file=sys.stderr)
        return 8
    print(f"            {len(plans)} clips loaded")
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    return _run_export(cfg, plans, timer=timer)


def run(
    cfg: PipelineConfig,
    *,
    from_plan: Path | None,
    debug: bool,
    chat_log_path: Path | None,
    weights: Weights | None,
) -> int:
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    timer = RunTimer()
    try:
        if from_plan is not None:
            return _run_from_plan(cfg, from_plan, timer=timer)
        return _run_full_pipeline(
            cfg,
            debug=debug,
            chat_log_path=chat_log_path,
            weights=weights,
            timer=timer,
        )
    finally:
        # Always emit timing — even on error returns and exceptions — so a
        # slow probe / detect can be diagnosed from the artefact.
        if timer.stages:
            try:
                _write_json(cfg.output_dir / "run_timing.json", timer.to_dict())
            except OSError:
                pass  # best-effort — don't mask the original error


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.list_detectors:
        for name in AVAILABLE_DETECTORS:
            print(name)
        return 0

    if args.input is None or args.output is None:
        print("ERROR: --input and --output are required (unless --list-detectors).", file=sys.stderr)
        return 1

    detector_name = args.detector
    if args.interactive_weights and detector_name != "composite":
        print(
            "INFO: --interactive-weights given; switching --detector to 'composite'.",
            file=sys.stderr,
        )
        detector_name = "composite"

    weights = _resolve_weights(args)

    cfg = PipelineConfig(
        input_path=args.input,
        output_dir=args.output,
        detector=detector_name,
        candidate_count=args.candidates,
        candidate_duration=args.window,
        min_clip_duration=args.min_duration,
        max_clip_duration=args.max_duration,
        export_clips=args.export_clips or args.from_plan is not None,
    )
    return run(
        cfg,
        from_plan=args.from_plan,
        debug=args.debug,
        chat_log_path=args.chat_log,
        weights=weights,
    )


if __name__ == "__main__":
    raise SystemExit(main())
