# Multi-stage build keeps the final image small. The builder stage exists
# only to host pip; the runtime stage carries Python + ffmpeg + the source.
#
# Pin Python to the same minor version the CI matrix tests against (see
# .github/workflows/test.yml).

FROM python:3.12-slim

# ffmpeg / ffprobe are runtime requirements (audio_rms, clip_exporter,
# video_info). --no-install-recommends keeps the image lean.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first so dependency installs cache across source edits.
# Currently installs yt-dlp + chat-downloader for the optional URL-ingest
# step (scripts/fetch.py / `--url`). The core pipeline doesn't need them.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Source last — every code edit only invalidates this layer.
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY tests/ ./tests/
COPY weights.example.json ./

# Make samples/ and output/ available even when host volumes aren't mounted.
RUN mkdir -p samples output

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# `docker compose run app --input ... --output ...` flows the args straight
# into the CLI. Override with `--entrypoint` for tests / shell.
ENTRYPOINT ["python", "-m", "src.main"]
CMD ["--help"]
