#!/usr/bin/env bash
# Starts PropRisk on this Mac: a private local PostgreSQL cluster, the
# FastAPI server (API + built screens) and the background worker, then
# opens the app in the browser. Safe to run again — it does nothing if
# PropRisk is already running. See README.md for what each step does.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
WEB_DIR="$ROOT_DIR/web"
DATA_DIR="$ROOT_DIR/data"
RUN_DIR="$DATA_DIR/run"
LOG_DIR="$DATA_DIR/logs"
PG_DIR="$DATA_DIR/postgres"

PORT="${PROPRISK_PORT:-8765}"
DB_PORT="${PROPRISK_DB_PORT:-55432}"
APP_URL="http://127.0.0.1:${PORT}"
# A short, fixed-length socket directory: PostgreSQL's Unix-domain socket
# path has a ~103-byte limit, and a project checked out somewhere deeply
# nested (long $ROOT_DIR) can blow past that inside data/run.
SOCKET_DIR="/tmp/proprisk-$(echo -n "$ROOT_DIR" | shasum | cut -c1-12)"

log() { echo "[launch] $*"; }

# 1. Mac only.
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "PropRisk's scripts run on a Mac only."
  exit 1
fi

# 2. PATH + prerequisites.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
# A shell with no locale set can make Homebrew's PostgreSQL fail to start
# ("postmaster became multithreaded during startup") on macOS.
export LC_ALL="${LC_ALL:-en_US.UTF-8}"
PG_PREFIX="$(ls -d /opt/homebrew/opt/postgresql@* 2>/dev/null | sort -V | tail -n1 || true)"
if [[ -n "$PG_PREFIX" ]]; then
  export PATH="$PG_PREFIX/bin:$PATH"
fi

missing=()
command -v uv >/dev/null 2>&1 || missing+=("uv|brew install uv")
if command -v node >/dev/null 2>&1; then
  node_major="$(node --version | sed -E 's/^v([0-9]+).*/\1/')"
  [[ "$node_major" -ge 20 ]] || missing+=("node (20 or newer)|brew install node")
else
  missing+=("node|brew install node")
fi
command -v npm >/dev/null 2>&1 || missing+=("npm|brew install node")
command -v initdb >/dev/null 2>&1 || missing+=("postgresql (initdb)|brew install postgresql@14")
command -v pg_ctl >/dev/null 2>&1 || missing+=("postgresql (pg_ctl)|brew install postgresql@14")
if command -v postgres >/dev/null 2>&1; then
  pg_major="$(postgres --version | grep -oE '[0-9]+' | head -n1)"
  [[ "$pg_major" -ge 14 ]] || missing+=("postgresql (14 or newer)|brew install postgresql@14")
else
  missing+=("postgresql|brew install postgresql@14")
fi
if ((${#missing[@]})); then
  echo "PropRisk needs the following before it can launch:"
  for entry in "${missing[@]}"; do
    name="${entry%%|*}"; cmd="${entry##*|}"
    echo "  - $name — run: $cmd"
  done
  exit 1
fi

mkdir -p "$RUN_DIR" "$LOG_DIR" "$SOCKET_DIR"

is_live() {
  local pidfile="$1"
  [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null
}

health_ok() {
  curl -fsS -m 2 "$APP_URL/api/health" >/dev/null 2>&1
}

# 3. Already running? The newest database upgrade is this version's schema;
# the running app recorded the one it started with.
SCHEMA_HEAD="$(basename "$(ls "$BACKEND_DIR"/alembic/versions/0*.py | sort | tail -1)" .py)"
if { is_live "$RUN_DIR/server.pid" || is_live "$RUN_DIR/worker.pid"; } \
    && [[ "$(cat "$RUN_DIR/schema_head" 2>/dev/null)" != "$SCHEMA_HEAD" ]]; then
  # An older PropRisk (server or worker) is still running: its database must
  # not be upgraded live.
  log "Stop PropRisk first (scripts/stop.sh), then launch again."
  exit 1
fi
if is_live "$RUN_DIR/server.pid" && is_live "$RUN_DIR/worker.pid" && health_ok; then
  log "PropRisk is already running at $APP_URL"
  open "$APP_URL"
  exit 0
fi
rm -f "$RUN_DIR/server.pid" "$RUN_DIR/worker.pid"

port_taken_by_other() {
  local port="$1"
  lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
}

# 4. Port conflicts.
if port_taken_by_other "$PORT"; then
  echo "Port $PORT is already used by another program."
  echo "Stop that program, or set PROPRISK_PORT to a free port and run this again."
  exit 1
fi
if [[ ! -d "$PG_DIR" ]] && port_taken_by_other "$DB_PORT"; then
  echo "Port $DB_PORT is already used by another program."
  echo "Stop that program, or set PROPRISK_DB_PORT to a free port and run this again."
  exit 1
fi

# 5. Build when needed (first run, or when sources/locks changed).
BUILD_STAMP="$RUN_DIR/web_build.sha256"
CURRENT_HASH="$(
  { find "$WEB_DIR/src" -type f -print0 | sort -z | xargs -0 shasum -a 256;
    shasum -a 256 "$WEB_DIR/index.html" "$WEB_DIR/package-lock.json" \
      "$BACKEND_DIR/pyproject.toml" "$BACKEND_DIR/uv.lock"; } \
  | shasum -a 256 | awk '{print $1}'
)"
PREVIOUS_HASH="$(cat "$BUILD_STAMP" 2>/dev/null || true)"

if [[ ! -d "$BACKEND_DIR/.venv" ]] || [[ "$CURRENT_HASH" != "$PREVIOUS_HASH" ]] || [[ ! -d "$WEB_DIR/dist" ]]; then
  log "Installing/updating the backend environment…"
  (cd "$BACKEND_DIR" && uv sync --frozen)
  log "Building the screens…"
  (cd "$WEB_DIR" && npm ci && npm run build)
  echo "$CURRENT_HASH" > "$BUILD_STAMP"
fi

# 6. First-run database cluster.
if [[ ! -d "$PG_DIR" ]]; then
  log "Creating the local database cluster…"
  initdb -D "$PG_DIR" -U proprisk --auth=trust >/dev/null
fi

# 7. Start PostgreSQL.
if ! pg_ctl -D "$PG_DIR" status >/dev/null 2>&1; then
  log "Starting the local database…"
  pg_ctl -D "$PG_DIR" -l "$LOG_DIR/postgres.log" \
    -o "-p $DB_PORT -k $SOCKET_DIR -c listen_addresses=127.0.0.1" start -w
fi
if ! PGPASSWORD= psql -h 127.0.0.1 -p "$DB_PORT" -U proprisk -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname='proprisk'" | grep -q 1; then
  createdb -h 127.0.0.1 -p "$DB_PORT" -U proprisk proprisk
fi

# 8. Migrate and make sure the secret store exists.
log "Preparing the database…"
(cd "$BACKEND_DIR" && \
  DATABASE_URL="postgresql+psycopg://proprisk@127.0.0.1:${DB_PORT}/proprisk" \
  uv run alembic upgrade head)
(cd "$BACKEND_DIR" && uv run python -c \
  "from app.core import secret_store; secret_store.list_secrets()")
echo "$SCHEMA_HEAD" > "$RUN_DIR/schema_head"

# 9. Start the server and the worker.
log "Starting PropRisk…"
(
  cd "$BACKEND_DIR"
  DATABASE_URL="postgresql+psycopg://proprisk@127.0.0.1:${DB_PORT}/proprisk" \
  PROPRISK_PORT="$PORT" \
  nohup uv run uvicorn app.main:app --host 127.0.0.1 --port "$PORT" \
    >> "$LOG_DIR/server.log" 2>&1 &
  echo $! > "$RUN_DIR/server.pid"
)
(
  cd "$BACKEND_DIR"
  DATABASE_URL="postgresql+psycopg://proprisk@127.0.0.1:${DB_PORT}/proprisk" \
  nohup uv run python -m app.worker \
    >> "$LOG_DIR/worker.log" 2>&1 &
  echo $! > "$RUN_DIR/worker.pid"
)

# 10. Wait for health, then open the browser.
for _ in $(seq 1 60); do
  if health_ok; then
    log "PropRisk is ready at $APP_URL"
    open "$APP_URL"
    exit 0
  fi
  sleep 1
done

echo "PropRisk did not start within 60 seconds. Last log lines:"
tail -n 20 "$LOG_DIR/server.log" 2>/dev/null || true
if is_live "$RUN_DIR/server.pid"; then kill "$(cat "$RUN_DIR/server.pid")" 2>/dev/null || true; fi
if is_live "$RUN_DIR/worker.pid"; then kill "$(cat "$RUN_DIR/worker.pid")" 2>/dev/null || true; fi
rm -f "$RUN_DIR/server.pid" "$RUN_DIR/worker.pid"
exit 1
