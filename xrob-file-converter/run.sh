#!/bin/sh

mkdir -p /data/uploads
mkdir -p /data/output
mkdir -p /data/tmp

exec uvicorn app:app \
    --host 0.0.0.0 \
    --port 3000
