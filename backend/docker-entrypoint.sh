#!/bin/sh
set -e

# Wait for postgres to be ready (retry loop, not blind sleep)
echo "Waiting for postgres..."
until python -c "import django; django.setup(); from django.db import connections; connections['default'].ensure_connection()" 2>/dev/null; do
  echo "  postgres not ready, retrying in 2s..."
  sleep 2
done
echo "Postgres is ready."

echo "Running migrations on all databases..."
python manage.py migrate --database=default
python manage.py migrate --database=shard_0
python manage.py migrate --database=shard_1
python manage.py migrate --database=idempotency_db

echo "Seeding data..."
python manage.py seed

echo "Starting server..."
exec "$@"
