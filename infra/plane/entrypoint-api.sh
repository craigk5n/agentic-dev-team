#!/bin/bash
# Drop-in replacement for the Plane API entrypoint that tolerates kombu
# broadcast errors in register_instance / configure_instance / clear_cache.
# The DB-write steps still succeed; only the Celery fan-out fails.
set -e

# Reproduce machine-signature computation from the upstream entrypoint
HOSTNAME=$(hostname)
MAC_ADDRESS=$(ip link show | awk '/ether/ {print $2}' | head -n 1)
CPU_INFO=$(cat /proc/cpuinfo)
MEMORY_INFO=$(free -h)
DISK_INFO=$(df -h)
SIGNATURE=$(echo "$HOSTNAME$MAC_ADDRESS$CPU_INFO$MEMORY_INFO$DISK_INFO" | sha256sum | awk '{print $1}')
export MACHINE_SIGNATURE=$SIGNATURE

python manage.py wait_for_db
python manage.py wait_for_migrations

# These commands write to the DB first, then attempt a Celery broadcast.
# The broadcast fails on stable with kombu 5.4 (ChannelPromise.__value__ removed).
# || true keeps the container alive; the DB writes succeed.
python manage.py register_instance "$MACHINE_SIGNATURE" || true
python manage.py configure_instance || true

python manage.py create_bucket
python manage.py clear_cache || true
python manage.py collectstatic --noinput

exec gunicorn \
  -w "${GUNICORN_WORKERS:-2}" \
  -k uvicorn.workers.UvicornWorker \
  plane.asgi:application \
  --bind "0.0.0.0:${PORT:-8000}" \
  --max-requests 1200 \
  --max-requests-jitter 1000 \
  --access-logfile -
