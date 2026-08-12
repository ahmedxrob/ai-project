FROM ghcr.io/home-assistant/base:latest

WORKDIR /app

RUN apk add --no-cache \
    python3 \
    py3-pip

COPY requirements.txt /tmp/requirements.txt

RUN python3 -m pip install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

COPY app ./app

RUN mkdir -p /data

EXPOSE 8099

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099"]
