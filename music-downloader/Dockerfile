FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
        gosu \
    && DENO_INSTALL=/usr/local curl -fsSL https://deno.land/install.sh | sh \
    && /usr/local/bin/deno --version \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system xrob \
    && useradd --system --gid xrob --home-dir /app --no-create-home xrob

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/xrob-music-entrypoint
RUN chmod 0755 /usr/local/bin/xrob-music-entrypoint \
    && chown -R xrob:xrob /app

EXPOSE 8099

ENTRYPOINT ["/usr/local/bin/xrob-music-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099"]
