FROM ghcr.io/home-assistant/base:latest

RUN apk add --no-cache     python3     py3-pip     curl     jq     ca-certificates     wget

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --break-system-packages --no-cache-dir -r /tmp/requirements.txt

COPY app.py /app/app.py
COPY run.sh /run.sh

RUN chmod +x /run.sh && mkdir -p /data /config /app

LABEL   io.hass.version="1.0.0"   io.hass.type="app"   io.hass.arch="amd64|aarch64"

CMD ["/run.sh"]
