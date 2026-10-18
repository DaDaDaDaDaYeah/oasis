# Oasis backend (FastAPI + headless Chromium for scraping + ffmpeg).
# Build context is ./backend; see docker-compose.yml and DOCKER.md at the repo root.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# chromium + chromium-driver come from the same Debian release, so the driver always
# matches the browser (no runtime driver download). tini reaps the Chrome and
# worker processes the backend spawns.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium chromium-driver ffmpeg tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN chmod +x docker-entrypoint.sh

ENV CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver \
    HOME=/tmp \
    DB_PATH=/data/oasis.db \
    OASIS_MEDIA_ROOT=/data \
    OASIS_SITES_DIR=/data/sites \
    OASIS_CORS_SAME_HOST=1 \
    OASIS_REMOTE_ACCESS=1

# Everything the user owns (database, movies/, adapters, access code) lives here.
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:8000/api/health', headers={'X-Oasis-Client': '1'}), timeout=4)" || exit 1

ENTRYPOINT ["tini", "--", "./docker-entrypoint.sh"]
