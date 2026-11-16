#!/usr/bin/env bash
# Runs PropRisk's whole offline test suite: the backend's pytest suite
# (which starts its own throwaway PostgreSQL cluster when DATABASE_URL
# isn't already set — see backend/tests/conftest.py), then the screens'
# build and smoke tests against recorded fixtures. No network calls. Exits
# with the tests' own status.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
WEB_DIR="$ROOT_DIR/web"

export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
PG_PREFIX="$(ls -d /opt/homebrew/opt/postgresql@* 2>/dev/null | sort -V | tail -n1 || true)"
if [[ -n "$PG_PREFIX" ]]; then
  export PATH="$PG_PREFIX/bin:$PATH"
fi
# A shell with no locale set can make Homebrew's PostgreSQL fail to start
# ("postmaster became multithreaded during startup") on macOS.
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

echo "[test] backend: uv run pytest…"
(cd "$BACKEND_DIR" && uv run pytest)
backend_status=$?

echo "[test] screens: npm run build…"
(
  cd "$WEB_DIR"
  npm ci
  npm run build
)
web_build_status=$?

web_status=$web_build_status
if [[ $web_build_status -eq 0 ]]; then
  echo "[test] screens: npm run test (against a throwaway local instance)…"
  SMOKE_DB_PORT="${PROPRISK_TEST_DB_PORT2:-$(( (RANDOM % 5000) + 51000 ))}"
  SMOKE_APP_PORT="${PROPRISK_TEST_APP_PORT:-$(( (RANDOM % 5000) + 48000 ))}"
  TMP_SMOKE_PG="$(mktemp -d /tmp/proprisk_test_smoke_pg.XXXXXX)"
  TMP_SMOKE_SOCK="$(mktemp -d /tmp/proprisk_test_smoke_sock.XXXXXX)"
  initdb -D "$TMP_SMOKE_PG" -U proprisk --auth=trust >/dev/null
  pg_ctl -D "$TMP_SMOKE_PG" -l "$TMP_SMOKE_PG/postgres.log" \
    -o "-p $SMOKE_DB_PORT -k $TMP_SMOKE_SOCK -c listen_addresses=127.0.0.1" start -w >/dev/null
  createdb -h 127.0.0.1 -p "$SMOKE_DB_PORT" -U proprisk proprisk_smoke
  SMOKE_DATABASE_URL="postgresql+psycopg://proprisk@127.0.0.1:${SMOKE_DB_PORT}/proprisk_smoke"
  (cd "$BACKEND_DIR" && DATABASE_URL="$SMOKE_DATABASE_URL" uv run alembic upgrade head >/dev/null)
  (
    cd "$BACKEND_DIR"
    DATABASE_URL="$SMOKE_DATABASE_URL" PROPRISK_DATA_DIR="$TMP_SMOKE_PG/data" \
      uv run uvicorn app.main:app --host 127.0.0.1 --port "$SMOKE_APP_PORT" \
      >"$TMP_SMOKE_PG/server.log" 2>&1 &
    echo $! > "$TMP_SMOKE_PG/server.pid"
  )
  for _ in $(seq 1 30); do
    curl -fsS "http://127.0.0.1:${SMOKE_APP_PORT}/api/health" >/dev/null 2>&1 && break
    sleep 1
  done
  (cd "$WEB_DIR" && SMOKE_BASE_URL="http://127.0.0.1:${SMOKE_APP_PORT}" npm run test)
  web_status=$?
  kill "$(cat "$TMP_SMOKE_PG/server.pid")" 2>/dev/null || true
  pg_ctl -D "$TMP_SMOKE_PG" stop -m fast >/dev/null 2>&1 || true
  rm -rf "$TMP_SMOKE_PG" "$TMP_SMOKE_SOCK"
fi

if [[ $backend_status -ne 0 ]]; then
  exit $backend_status
fi
exit $web_status
