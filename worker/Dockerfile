FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY worker/ ./worker/

ENV PYTHONPATH=/app/backend
ENV DJANGO_SETTINGS_MODULE=config.settings

# Run as non-root user — least-privilege principle
RUN addgroup --system celery && adduser --system --ingroup celery celery
USER celery

CMD ["celery", "-A", "worker.worker_app", "worker", "--loglevel=info", "-B"]
