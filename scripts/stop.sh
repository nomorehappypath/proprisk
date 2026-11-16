#!/usr/bin/env bash
# Stops everything scripts/launch.sh started: the worker, the server, then
# the local database. Always exits 0 — "nothing was running" is not an
# error. A lookup cut off here is picked up by the existing stale-job
# recovery the next time launch.sh runs.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$ROOT_DIR/data"
RUN_DIR="$DATA_DIR/run"
PG_DIR="$DATA_DIR/postgres"

export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
PG_PREFIX="$(ls -d /opt/homebrew/opt/postgresql@* 2>/dev/null | sort -V | tail -n1 || true)"
if [[ -n "$PG_PREFIX" ]]; then
  export PATH="$PG_PREFIX/bin:$PATH"
fi

stopped_anything=0

stop_pid_file() {
  local name="$1" pidfile="$2" graceful_seconds="$3"
  if [[ ! -f "$pidfile" ]]; then
    return
  fi
  local pid
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 "$graceful_seconds"); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
    echo "[stop] stopped $name"
    stopped_anything=1
  fi
  rm -f "$pidfile"
}

stop_pid_file "worker" "$RUN_DIR/worker.pid" 30
stop_pid_file "server" "$RUN_DIR/server.pid" 10

if [[ -d "$PG_DIR" ]] && pg_ctl -D "$PG_DIR" status >/dev/null 2>&1; then
  pg_ctl -D "$PG_DIR" stop -m fast >/dev/null 2>&1 || true
  echo "[stop] stopped the local database"
  stopped_anything=1
fi

if [[ "$stopped_anything" -eq 0 ]]; then
  echo "PropRisk was not running."
fi
exit 0
