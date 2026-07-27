#!/bin/sh
set -e

echo "Waiting for Postgres at $DB_HOST:$DB_PORT..."
until pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -q; do
  sleep 1
done
echo "Postgres is ready."

echo "Running database migrations..."
cd /app/backend
alembic upgrade head

echo "Starting server..."
cd /app
# SINGLE WORKER ONLY — do NOT add `--workers N` here, and do not run more than
# one container of this image (P2 decision, 2026-07-27).
#
# services/rate_limiter.py holds its sliding windows in process memory, and that
# store backs the S1 login brute-force + spray guards, the S6 client-error DoS
# guard, and the #39 flood guard — not just the LLM budget. N workers => every
# one of those limits becomes N times looser (LOGIN_MAX_ATTEMPTS=10 -> 10N per
# account). That is a security regression, not a cost issue.
#
# Note the `alembic upgrade head` above: multiple containers race on migrations
# too, which is a separate failure from multiple workers and hits first.
#
# Horizontal scale is unblocked by a shared (Redis) rate-limit backend —
# see docs/SECURITY_ARCHITECTURE_DECISIONS.md §3.
exec uvicorn backend.main:app --host 0.0.0.0 --port 8000
