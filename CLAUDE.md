# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Long-form video → hotspot detection → clip extraction pipeline. See [README.md](README.md) and [docs/project_overview.md](docs/project_overview.md) for the full goal.

**Current state**: pipeline is wired end-to-end with two detectors:
- `even` — placeholder (evenly-spaced windows, score=0.5).
- `audio_rms` — real signal: extracts mono PCM via ffmpeg, computes per-second RMS in Python, picks top-K with NMS.

`--from-plan` re-export and `--debug` artefact dumps are wired. Tests run via stdlib unittest. Real (not synthetic) video has not been tried yet — that's the next human-judgment step.

## Common commands

```bash
# Plan only (recommended first run)
python -m src.main --input samples/sample.mp4 --output output/ \
    --detector audio_rms --candidates 6 --window 30 --debug

# Full pipeline including encode
python -m src.main --input samples/sample.mp4 --output output/ \
    --detector audio_rms --export-clips

# Edit clip_plan.json by hand, then re-export only
python -m src.main --input samples/sample.mp4 --output output/ \
    --from-plan output/clip_plan.json

# Synthetic test fixture
ffmpeg -y -f lavfi -i testsrc=duration=120:size=320x240:rate=30 \
       -f lavfi -i sine=frequency=440:duration=120 \
       -c:v libx264 -preset ultrafast -c:a aac -shortest samples/sample.mp4

# Tests (no install needed)
python -m unittest discover -s tests
```

External requirement: `ffmpeg` and `ffprobe` on PATH. Zero Python deps.

## Architecture

Strict one-way module dependency, top to bottom:

```
main.py (CLI)
  → config.py (PipelineConfig dataclass)
  → video_info.py     (ffprobe wrapper, exit codes 2-4)
  → hotspot_detector.py (HotspotDetector ABC + EvenSampling + AudioRms; exit 6)
  → clip_planner.py   (HotspotCandidate → ClipPlan)
  → clip_exporter.py  (ffmpeg encode, off by default; exit 5)
```

Detector contract:
```python
detect(*, input_path: Path, duration: float, debug_dir: Path | None = None)
    -> list[HotspotCandidate]
```

Each stage writes a JSON artefact (`video_info.json`, `hotspot_candidates.json`, `clip_plan.json`, `clip_export_result.json`, `debug/audio_rms.json`) so any stage can be re-run independently. See [docs/architecture.md](docs/architecture.md) for the rationale.

## Conventions worth knowing

- **Subprocess to ffmpeg/ffprobe, no Python video bindings.** Done deliberately to keep `requirements.txt` empty. Don't add `ffmpeg-python`/`av`/`moviepy`/`numpy` without discussing — see [docs/tasks.md](docs/tasks.md) design log entry "音声 RMS 抽出は ffmpeg → PCM → Python で計算".
- **`AudioRmsDetector` deliberately uses raw PCM, not ffmpeg `astats` text parsing.** astats output format is fragile across ffmpeg versions; PCM is rock-solid. Don't switch back without reading the design log.
- **`--export-clips` is opt-in.** Default run only writes JSON. `--from-plan` implies `--export-clips`.
- **All ffmpeg/ffprobe failures map to typed exceptions** (`FFprobeNotFoundError`, `FFprobeFailedError`, `FFmpegNotFoundError`, `AudioExtractionError`) → main maps to exit codes 2–8.
- **Tests use stdlib `unittest` + `unittest.mock`.** No pytest dependency. Add new tests in the same style; run with `python -m unittest discover -s tests`.

## Repo / remote note

Local directory is `video-workflow/` but the intended GitHub remote (per `docs.md`) is `video-workflow-agent`. No `git init` has been run yet. Confirm with the user before pushing or renaming.
