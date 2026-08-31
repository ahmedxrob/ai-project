#!/bin/sh

set -e

mkdir -p \
    /data/uploads \
    /data/output \
    /data/tmp

exec uvicorn app:app \
    --host 0.0.0.0 \
    --port 3000
