FROM ghcr.io/home-assistant/base:latest

RUN apk add --no-cache \
    python3 \
    py3-pip \
    curl \
    jq \
    ca-certificates \
    wget

COPY requirements.txt /tmp/requirements.txt

RUN pip3 install \
    --break-system-packages \
    --no-cache-dir \
    -r /tmp/requirements.txt

RUN mkdir -p /app/static /data /config

COPY app.py /app/app.py
COPY static /app/static
COPY run.sh /run.sh

RUN chmod +x /run.sh

LABEL io.hass.version="1.1.6"
LABEL io.hass.type="app"
LABEL io.hass.arch="amd64|aarch64"

EXPOSE 8055

CMD ["/run.sh"]

