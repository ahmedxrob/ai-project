FROM ghcr.io/home-assistant/base:latest

RUN apk add --no-cache \
    python3 \
    openssh-client \
    sshpass

COPY run.sh /run.sh
COPY server.py /server.py

RUN chmod 755 /run.sh /server.py

CMD ["/run.sh"]
