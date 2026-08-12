#!/usr/bin/with-contenv bashio

export TMDB_TOKEN="$(bashio::config 'tmdb_token')"

exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8099
