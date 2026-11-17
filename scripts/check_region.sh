#!/usr/bin/env bash
# The one proof command for a state or county file. See docs/EXTENDING.md.
#   scripts/check_region.sh tx           check Texas and every Texas county
#   scripts/check_region.sh tx comal     check one county
# Needs no running app and no keys. Ends with PASS or FAIL.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
PG_PREFIX="$(ls -d /opt/homebrew/opt/postgresql@* 2>/dev/null | sort -V | tail -n1 || true)"
if [[ -n "$PG_PREFIX" ]]; then
  export PATH="$PG_PREFIX/bin:$PATH"
fi
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

cd "$ROOT_DIR/backend" && exec uv run python -m app.cli.check_region "$@"
