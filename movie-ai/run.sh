#!/bin/sh

TMDB_TOKEN="$(python -c "import json; print(json.load(open('/data/options.json')).get('tmdb_token',''))")"
GEMINI_API_KEY="$(python -c "import json; print(json.load(open('/data/options.json')).get('gemini_api_key',''))")"

export TMDB_TOKEN
export GEMINI_API_KEY

if [ -n "$TMDB_TOKEN" ]; then
    echo "TMDB token loaded successfully"
else
    echo "WARNING: TMDB token is not configured"
fi

if [ -n "$GEMINI_API_KEY" ]; then
    echo "Gemini API key loaded successfully"
else
    echo "WARNING: Gemini API key is not configured"
fi

exec python -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8099
