ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.14-alpine3.24
FROM ${BUILD_FROM}
WORKDIR /app
COPY rootfs/ /app/
RUN pip install --no-cache-dir -r /app/requirements.txt
RUN chmod a+x /app/run.sh
CMD ["/app/run.sh"]
