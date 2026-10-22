# Jinora container image.
# - Only runtime code is copied (src/, config/, prompts/, main.py, start_server.py); .dockerignore keeps .env, data/,
#   downloads/, logs/, calibration videos and caches out of the build context. Secrets arrive at run time (env_file).
# - No ffmpeg: Gemini handles audio and video, libmediainfo only reads container metadata.
# - Runs as an unprivileged user; HOST=0.0.0.0 applies only inside the container (the compose file publishes the port
#   on 127.0.0.1 of the host).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libmediainfo0v5: required by pymediainfo. Nothing else (no ffmpeg, no compilers, no git).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libmediainfo0v5 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 jinora \
    && useradd --system --uid 10001 --gid jinora --create-home --home-dir /home/jinora jinora

WORKDIR /app

# Dependencies first (cached layer). requirements.txt pins exact versions.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=jinora:jinora main.py start_server.py ./
COPY --chown=jinora:jinora src ./src
COPY --chown=jinora:jinora config ./config
COPY --chown=jinora:jinora prompts ./prompts

# Writable state lives in mounted volumes (see docker-compose.yml); the empty directories make a bare `docker run` work too.
RUN mkdir -p data/backup downloads logs reports \
    && chown -R jinora:jinora /app

USER jinora

ENV HOST=0.0.0.0 \
    PORT=8000 \
    ENV=prod \
    DATABASE_URL=sqlite:///data/meta_ads.db

EXPOSE 8000

# Lightweight probe: the API answers and the database responds.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8000'), timeout=4)"]

CMD ["python", "start_server.py"]
