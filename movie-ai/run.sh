#!/bin/sh
set -eu

OPTIONS_FILE="${OPTIONS_FILE:-/data/options.json}"

if [ -f "$OPTIONS_FILE" ]; then
    TMDB_TOKEN="$(python -c "import json; print(json.load(open('$OPTIONS_FILE')).get('tmdb_token',''))")"
    GEMINI_API_KEY="$(python -c "import json; print(json.load(open('$OPTIONS_FILE')).get('gemini_api_key',''))")"
else
    TMDB_TOKEN="${TMDB_TOKEN:-}"
    GEMINI_API_KEY="${GEMINI_API_KEY:-}"
    echo "INFO: $OPTIONS_FILE not found; using environment variables."
fi

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

exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8099
