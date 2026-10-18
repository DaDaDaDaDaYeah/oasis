#!/bin/sh
set -e

# Seed the writable adapter directory from the ones shipped in the image. Shipped
# adapters are refreshed on every start (so an image update updates them); any
# other file in /data/sites is the user's own and is left alone -- the same
# contract run_backend.py gives the desktop build.
mkdir -p /data/sites /data/movies
if [ -d /app/sites ]; then
    cp -f /app/sites/*.json /data/sites/ 2>/dev/null || true
fi

exec python -m uvicorn api:app --host 0.0.0.0 --port 8000
