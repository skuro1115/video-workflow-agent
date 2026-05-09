# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Long-form video → hotspot detection → clip extraction pipeline. See [README.md](README.md) and [docs/project_overview.md](docs/project_overview.md) for the full goal.

**Current state**: pipeline runs end-to-end with 4 detectors:
- `even` — placeholder (evenly-spaced windows, score=0.5)
- `audio_rms` — extracts mono PCM via ffmpeg, picks loudness peaks with NMS
- `comment_density` — bins live-chat messages, picks high-unique-user-count windows
- `composite` — runs multiple sub-detectors, weighted-sum combines per-bin scores

`--from-plan`, `--debug`, `--chat-log`, `--weights`, `--interactive-weights`, `--list-detectors` are wired. Tests via stdlib unittest. Real (not synthetic) video has not been tried yet — that's the next human-judgment step.

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
  → score_weights.py    (Weights dataclass, load/save/interactive)
  → config.py           (PipelineConfig dataclass)
  → video_info.py       (ffprobe wrapper, exit 2-4)
  → hotspot_detector.py (HotspotDetector ABC + 4 implementations + factory; exit 6/9/10)
  → clip_planner.py     (HotspotCandidate → ClipPlan)
  → clip_exporter.py    (ffmpeg encode, opt-in; exit 5)
```

Detector contract:
```python
detect(*, input_path: Path, duration: float, debug_dir: Path | None = None)
    -> list[HotspotCandidate]
```

Each stage writes a JSON artefact (`video_info.json`, `hotspot_candidates.json`, `clip_plan.json`, `clip_export_result.json`, `debug/audio_rms.json`, `debug/comment_density.json`, `debug/composite_combined.json`, `debug/composite_subdetectors.json`) so any stage can be re-run independently. Field-level reference: [docs/schemas.md](docs/schemas.md) — keep in sync when changing any dataclass. See [docs/architecture.md](docs/architecture.md) for the rationale.

## Conventions worth knowing

- **No Python video bindings.** Subprocess to `ffmpeg`/`ffprobe`. Don't add `ffmpeg-python`/`av`/`moviepy`/`numpy` without discussing the design log in [docs/tasks.md](docs/tasks.md).
- **`AudioRmsDetector` uses raw PCM, not ffmpeg `astats` parsing.** Astats text format is fragile across ffmpeg versions; PCM is rock-solid. See design log.
- **Score normalisation: min-max within each detector, then weighted sum.** RRF is on the roadmap as an alternative; don't change the current default without updating the design log.
- **CompositeDetector skips bins with zero contribution.** A bin nobody scored is not a candidate, even with `min_score=0`. Don't relax this without thinking — it reintroduces a real bug.
- **Single-candidate edge case: norm = 1.0, not 0.** When a sub-detector returns one candidate, min-max would degenerate to zero and silently drop it. The fallback is `if s_max <= s_min: norm = 1.0`. Don't remove the guard.
- **`--export-clips` is opt-in.** `--from-plan` implies it; nothing else does.
- **All ffmpeg/ffprobe failures map to typed exceptions** → main maps to exit codes 2–10 (see [docs/workflow.md](docs/workflow.md)).
- **Tests use stdlib `unittest` + `unittest.mock`.** No pytest. `python -m unittest discover -s tests`.

## Repo / remote note

Local directory is `video-workflow/`, GitHub remote is [skuro1115/video-workflow-agent](https://github.com/skuro1115/video-workflow-agent). `main` tracks `origin/main`. CI is wired in [.github/workflows/test.yml](.github/workflows/test.yml) — runs the unittest suite on Python 3.11/3.12 with ffmpeg installed, on every push to main and every PR.
