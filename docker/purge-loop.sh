#!/bin/sh
# Nightly SyncSession retention.
#
# A sleep loop rather than cron: the image carries no cron daemon, this keeps
# the schedule and the retention window next to the rest of the compose
# config, and every run lands in `docker compose logs purge` instead of a
# logfile inside a container nobody looks in.
#
# Deliberately not started through docker/entrypoint.sh -- that runs
# `manage.py migrate`, and two containers racing migrations at boot is its own
# problem. The compose service waits on web's healthcheck instead, by which
# point migrations have already run.
set -e

DAYS="${PURGE_RETENTION_DAYS:-30}"
AT="${PURGE_AT:-03:15}"

echo "purge: keeping ${DAYS} days of sync sessions, running daily at ${AT} $(date +%Z)"

# Time until the next occurrence of $AT, in the container's timezone (UTC
# unless TZ is set). Recomputed each night rather than sleeping a fixed 86400
# so the run time cannot drift and a DST shift costs at most one hour.
seconds_until() {
  python - "$1" <<'EOF'
import datetime as dt
import sys

hour, _, minute = sys.argv[1].partition(":")
now = dt.datetime.now()
target = now.replace(hour=int(hour), minute=int(minute or 0), second=0, microsecond=0)
if target <= now:
    target += dt.timedelta(days=1)
print(int((target - now).total_seconds()))
EOF
}

while true; do
  delay="$(seconds_until "$AT")"
  echo "purge: next run in ${delay}s"
  sleep "$delay"

  # A failed purge must not take the loop down with it: the window is a
  # rolling one, so tomorrow's run deletes everything today's would have.
  if ! python manage.py purge_sync_sessions --days "$DAYS"; then
    echo "purge: run failed; retrying at ${AT} tomorrow" >&2
  fi
done
