FROM ghcr.io/home-assistant/base:latest

RUN apk add --no-cache \
    python3 \
    py3-pip \
    jq

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --break-system-packages -r /tmp/requirements.txt

COPY inverter.py /app/inverter.py
COPY run.sh /run.sh

RUN chmod +x /run.sh

RUN wget -O /app/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 && \
    chmod +x /app/cloudflared

LABEL \
    io.hass.version="2.0.0" \
    io.hass.type="app" \
    io.hass.arch="amd64"

CMD ["/run.sh"]
