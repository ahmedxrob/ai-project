ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.14-alpine3.24
FROM ${BUILD_FROM}
WORKDIR /app

# Keep executable Python and browser JavaScript in unambiguous destinations.
COPY rootfs/main.py /app/main.py
COPY rootfs/mikasa_persona.py /app/mikasa_persona.py
COPY rootfs/run.sh /app/run.sh
COPY rootfs/requirements.txt /app/requirements.txt
COPY rootfs/www/ /app/www/
COPY icon.png /app/www/xrob-logo.png

RUN apk add --no-cache tzdata \
    && pip install --no-cache-dir -r /app/requirements.txt \
    && python -m py_compile /app/main.py /app/mikasa_persona.py
RUN chmod a+x /app/run.sh
CMD ["/app/run.sh"]
