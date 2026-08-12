#!/usr/bin/with-contenv bashio

TMDB_TOKEN="$(bashio::config 'tmdb_token')"
GEMINI_API_KEY="$(bashio::config 'gemini_api_key')"

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
