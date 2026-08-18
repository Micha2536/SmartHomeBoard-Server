FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SHB_DATA_DIR=/data \
    SHB_MODULE_DIR=/app/modules

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server ./server
COPY modules ./modules
EXPOSE 8400 8787
VOLUME ["/data", "/app/modules"]
CMD ["python", "-m", "server.launcher"]
