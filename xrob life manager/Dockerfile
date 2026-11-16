ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.20
FROM ${BUILD_FROM}
WORKDIR /app
COPY rootfs /
RUN pip install --no-cache-dir -r /app/requirements.txt
RUN chmod a+x /app/run.sh
CMD ["/app/run.sh"]
