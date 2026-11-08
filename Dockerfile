FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    DB_PATH=/data/app.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# Mount a volume at /data to keep the database across container replacements.
RUN mkdir -p /data

EXPOSE 8000
# Single process: the in-process reconciler and per-SKU locks live here, and every
# delivery guarantee is also enforced in SQLite, so restarts are safe.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
