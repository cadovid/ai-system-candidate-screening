#!/bin/sh
set -eu

: "${PORT:=8001}"
export PORT

alembic upgrade head
exec uvicorn candidate_screening.main:app --host 0.0.0.0 --port "$PORT" --workers 1
