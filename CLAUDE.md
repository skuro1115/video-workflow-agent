# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Long-form video → hotspot detection → clip extraction pipeline. See [README.md](README.md) and [docs/project_overview.md](docs/project_overview.md) for the full goal.

**Current state**: pipeline runs end-to-end with 5 detectors:
- `even` — placeholder (evenly-spaced windows, score=0.5)
- `audio_rms` — extracts mono PCM via ffmpeg, picks loudness peaks with NMS
- `comment_density` — bins live-chat messages, picks high-unique-user-count windows
- `comment_reaction` — like `comment_density`, but only counts reaction tokens (草 / lol / w連投 / 🤣). Sharper than density when chat is busy with greetings
- `composite` — runs multiple sub-detectors, weighted-sum combines per-bin scores

`--from-plan`, `--debug`, `--chat-log`, `--weights`, `--interactive-weights`, `--list-detectors`, `--settings`, `--url`, `--export-thumbnails` are wired. Tests via stdlib unittest. Real (not synthetic) video has not been tried yet — that's the next human-judgment step.

**Ingest** ([scripts/fetch.py](scripts/fetch.py)): URL → `<dir>/<name>.mp4` + `<dir>/<name>.chat.json` (app format). YouTube live archives use `yt-dlp --write-subs --sub-langs live_chat`; Twitch VODs use `chat-downloader` (yt-dlp doesn't extract Twitch chat). Triggered standalone (`python -m scripts.fetch --url ...`) or via `src.main --url ...` (lazy-imported so the core pipeline never touches yt-dlp). Exit codes 20–26 — see the module docstring.

**Environment + Docker**: see [SETUP.md](SETUP.md) — that's the single source of truth for required versions (Python 3.11+, ffmpeg 4+) and the canonical place to update when bumping versions. There's a [Dockerfile](Dockerfile) + [docker-compose.yml](docker-compose.yml) for users who'd rather not install ffmpeg locally.

**Non-engineer config**: [settings.example.json](settings.example.json) bundles all common options into one JSON; users copy it to `settings.json` and run `python -m src.main --settings settings.json`. CLI flags override file values. The loader is in [src/settings_loader.py](src/settings_loader.py).

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

# Evaluate a run against a hand-curated expected.json (planted peaks)
python -m scripts.eval \
    --hotspots output/hotspot_candidates.json \
    --expected samples/varying.expected.json

# Fetch from URL (YouTube live archive / Twitch VOD) → samples/
python -m scripts.fetch --url <URL> --output samples/ --name liveA

# One-shot: URL → fetch → full pipeline
python -m src.main --url <URL> --output output/ --detector composite \
    --weights weights.example.json
```

External requirements: `ffmpeg` and `ffprobe` on PATH (always); `yt-dlp` and `chat-downloader` (only for `scripts/fetch.py` / `--url`, installed via `pip install -r requirements.txt`).

## Architecture

Strict one-way module dependency, top to bottom:

```
main.py (CLI)
  → score_weights.py      (Weights dataclass, load/save/interactive)
  → config.py             (PipelineConfig dataclass)
  → video_info.py         (ffprobe wrapper, exit 2-4)
  → hotspot_detector.py   (HotspotDetector ABC + 5 implementations + factory; exit 6/9/10)
  → clip_planner.py       (HotspotCandidate → ClipPlan)
  → thumbnail_extractor.py(ffmpeg single-frame, opt-in via --export-thumbnails; exit 5)
  → clip_exporter.py      (ffmpeg encode, opt-in; exit 5)
```

Detector contract:
```python
detect(*, input_path: Path, duration: float, debug_dir: Path | None = None)
    -> list[HotspotCandidate]
```

Each stage writes a JSON artefact (`video_info.json`, `hotspot_candidates.json`, `clip_plan.json`, `clip_export_result.json`, `thumbnail_export_result.json`, `run_timing.json`, `debug/audio_rms.json`, `debug/comment_density.json`, `debug/comment_reaction.json`, `debug/composite_combined.json`, `debug/composite_subdetectors.json`) so any stage can be re-run independently. Field-level reference: [docs/schemas.md](docs/schemas.md) — keep in sync when changing any dataclass. See [docs/architecture.md](docs/architecture.md) for the rationale.

## Conventions worth knowing

- **No Python video bindings.** Subprocess to `ffmpeg`/`ffprobe`. Don't add `ffmpeg-python`/`av`/`moviepy`/`numpy` without discussing the design log in [docs/tasks.md](docs/tasks.md).
- **`AudioRmsDetector` uses raw PCM, not ffmpeg `astats` parsing.** Astats text format is fragile across ffmpeg versions; PCM is rock-solid. See design log.
- **Composite fusion: `weighted_sum` (default) or `rrf`.** `weighted_sum` does min-max normalise → weighted average and is intuitive but vulnerable to outlier scores. `rrf` discards score magnitudes and uses ranks, so one giant outlier can't dominate. Pick by detector mix, not just preference — see the design log in [docs/tasks.md](docs/tasks.md).
- **CompositeDetector skips bins with zero contribution.** A bin nobody scored is not a candidate, even with `min_score=0`. Don't relax this without thinking — it reintroduces a real bug.
- **Single-candidate edge case: norm = 1.0, not 0.** When a sub-detector returns one candidate, min-max would degenerate to zero and silently drop it. The fallback is `if s_max <= s_min: norm = 1.0`. Don't remove the guard.
- **`--export-clips` is opt-in.** `--from-plan` implies it; nothing else does.
- **`--export-thumbnails` is independent of `--export-clips`.** Cheap (~50ms/clip) and meant for visual review of `clip_plan.json` before paying the encode cost. `--from-plan` does NOT imply it (thumbnails should already exist from the planning run).
- **All ffmpeg/ffprobe failures map to typed exceptions** → main maps to exit codes 2–10 (see [docs/workflow.md](docs/workflow.md)).
- **Tests use stdlib `unittest` + `unittest.mock`.** No pytest. `python -m unittest discover -s tests`.
- **Ingest deps are optional.** `scripts/fetch.py` is imported lazily inside `src/main.py` only when `--url` is set, so a minimal install (no `yt-dlp` / `chat-downloader`) still runs the full pipeline. Don't move that import to module top-level.
- **Chat converters are pure functions.** `parse_youtube_live_chat_jsonl` / `parse_twitch_chat_json` take in-memory data, not paths. Keep them subprocess-free so `tests/test_fetch.py` runs without network or external CLIs.

## Repo / remote note

Local directory is `video-workflow/`, GitHub remote is [skuro1115/video-workflow-agent](https://github.com/skuro1115/video-workflow-agent). `main` tracks `origin/main`. CI is wired in [.github/workflows/test.yml](.github/workflows/test.yml) — runs the unittest suite on Python 3.11/3.12 with ffmpeg installed, on every push to main and every PR.
