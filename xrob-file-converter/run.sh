#!/bin/sh

set -e

mkdir -p /data/uploads
mkdir -p /data/output
mkdir -p /data/tmp

echo "============================================================"
echo " Xrob File Converter"
echo "============================================================"
echo "Starting web server on port 3000..."
echo "============================================================"

exec uvicorn app:app \
    --host 0.0.0.0 \
    --port 3000 \
    --workers 1
