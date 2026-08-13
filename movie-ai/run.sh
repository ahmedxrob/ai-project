#!/bin/sh

TMDB_TOKEN="$(python -c "import json; print(json.load(open('/data/options.json')).get('tmdb_token',''))")"

export TMDB_TOKEN

if [ -n "$TMDB_TOKEN" ]; then
    echo "TMDB token loaded successfully"
else
    echo "WARNING: TMDB token is not configured"
fi

exec python -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8099

