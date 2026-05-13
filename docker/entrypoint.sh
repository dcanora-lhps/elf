#!/bin/sh
set -e

if [ -z "$SANTA_SYNC_TOKEN" ]; then
  echo "WARNING: SANTA_SYNC_TOKEN is empty — sync endpoints will reject all requests with 500." >&2
fi

# Wait briefly for the database if a host is configured. Gunicorn will fail
# fast on the next migrate call if it never comes up.
if [ -n "$DATABASE_URL" ]; then
  echo "Running migrations against \$DATABASE_URL..."
fi
python manage.py migrate --noinput

exec "$@"
