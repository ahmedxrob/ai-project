#!/usr/bin/with-contenv bashio

set -e

echo "========================================="
echo " Xrob Port Publisher"
echo "========================================="

mkdir -p /data /config /app/static

ARCH="$(uname -m)"

case "$ARCH" in
    x86_64|amd64)
        CF_ARCH="amd64"
        ;;
    aarch64|arm64)
        CF_ARCH="arm64"
        ;;
    *)
        echo "Unsupported architecture: $ARCH"
        exit 1
        ;;
esac

CLOUDFLARED="/app/cloudflared"

if [ ! -x "$CLOUDFLARED" ]; then
    echo "Downloading cloudflared for $CF_ARCH..."

    wget -q --show-progress \
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" \
        -O "$CLOUDFLARED"

    chmod +x "$CLOUDFLARED"
fi

echo "cloudflared: $("$CLOUDFLARED" --version || true)"

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

echo "Starting Port Publisher UI on 8099..."

exec python3 /app/app.py

