#!/usr/bin/with-contenv bashio
set -e
export XROB_DATA_DIR="/config"
export XROB_TELEGRAM_TOKEN="$(bashio::config 'telegram_bot_token')"
export XROB_TELEGRAM_CHAT_ID="$(bashio::config 'telegram_allowed_chat_id')"
export TZ="$(bashio::config 'timezone')"
mkdir -p /config
exec python /app/main.py
