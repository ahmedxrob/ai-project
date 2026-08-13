FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt

RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY app ./app

COPY run.sh /run.sh

RUN sed -i 's/\r$//' /run.sh && chmod +x /run.sh

RUN mkdir -p /data

EXPOSE 8099

CMD ["/run.sh"]

