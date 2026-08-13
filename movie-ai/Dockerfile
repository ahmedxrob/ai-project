```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt

RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY app ./app

COPY run.sh /run.sh

# Remove Windows CRLF and UTF-8 BOM if present
RUN sed -i 's/\r$//' /run.sh && \
    sed -i '1s/^\xEF\xBB\xBF//' /run.sh && \
    chmod +x /run.sh

RUN mkdir -p /data

EXPOSE 8099

CMD ["sh", "/run.sh"]
```
