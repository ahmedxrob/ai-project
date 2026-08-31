FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

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

RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    python-multipart \
    aiofiles

WORKDIR /app

COPY app.py /app/app.py
COPY www /app/www
COPY run.sh /run.sh

RUN chmod +x /run.sh

RUN mkdir -p /data/uploads /data/output /data/tmp

EXPOSE 3000

CMD ["/run.sh"]
