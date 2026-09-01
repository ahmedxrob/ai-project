#!/usr/bin/with-contenv bashio
set -euo pipefail

export PC_IP="$(bashio::config 'pc_ip')"
export PC_USER="$(bashio::config 'username')"
export PC_PASS="$(bashio::config 'password')"
export API_TOKEN="$(bashio::config 'api_token')"

bashio::log.info "Xrob PC Sleep starting"
bashio::log.info "Target PC: ${PC_USER}@${PC_IP}"

exec python3 /server.py
