FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y \
    libreoffice \
    libreoffice-writer \
    libreoffice-calc \
    libreoffice-impress \
    imagemagick \
    ghostscript \
    ffmpeg \
    poppler-utils \
    ca-certificates \
    fonts-dejavu \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

RUN pip install \
    fastapi \
    "uvicorn[standard]" \
    python-multipart \
    aiofiles

WORKDIR /app

COPY app.py /app/app.py
COPY www /app/www
COPY run.sh /run.sh

RUN chmod +x /run.sh

RUN mkdir -p \
    /data/uploads \
    /data/output \
    /data/tmp

EXPOSE 3000

ARG BUILD_VERSION="1.0.0"
ARG BUILD_ARCH="amd64"

LABEL \
    io.hass.type="app" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.name="Xrob File Converter" \
    io.hass.description="Lightweight CPU-compatible file converter"

CMD ["/run.sh"]
