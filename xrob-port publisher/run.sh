#!/usr/bin/with-contenv bashio

set -e

echo "========================================="
echo " Xrob Port Publisher"
echo "========================================="

mkdir -p /data
mkdir -p /config
mkdir -p /app/static

# Detect CPU architecture
ARCH="$(uname -m)"

case "$ARCH" in
    x86_64|amd64)
        CF_ARCH="amd64"
        ;;
    aarch64|arm64)
        CF_ARCH="arm64"
        ;;
    *)
        echo "ERROR: Unsupported architecture: $ARCH"
        exit 1
        ;;
esac

# Persistent cloudflared location
CLOUDFLARED="/data/cloudflared"

# Download only when it does not already exist
if [ ! -x "$CLOUDFLARED" ]; then
    echo "Downloading cloudflared for ${CF_ARCH}..."

    TMP_CLOUDFLARED="/data/cloudflared.tmp"

    rm -f "$TMP_CLOUDFLARED"

    wget -q --show-progress \
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" \
        -O "$TMP_CLOUDFLARED"

    chmod +x "$TMP_CLOUDFLARED"

    mv "$TMP_CLOUDFLARED" "$CLOUDFLARED"

    echo "cloudflared downloaded successfully."
else
    echo "Using existing cloudflared from /data."
fi

echo "cloudflared: $("$CLOUDFLARED" --version || true)"

# Check UI files
echo "Checking web UI..."

if [ ! -f "/app/static/index.html" ]; then
    echo "ERROR: /app/static/index.html is missing!"
    exit 1
fi

if [ ! -f "/app/static/style.css" ]; then
    echo "ERROR: /app/static/style.css is missing!"
    exit 1
fi

if [ ! -f "/app/static/app.js" ]; then
    echo "ERROR: /app/static/app.js is missing!"
    exit 1
fi

echo "Web UI files found."

# Tell Flask which port to use
export PORT=8055

echo "Starting Port Publisher UI on ${PORT}..."

exec python3 /app/app.py
