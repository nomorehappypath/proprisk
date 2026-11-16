#!/usr/bin/env bash
# Manage PropRisk's API keys and secrets from the terminal, without opening
# the app. Everything is stored in data/proprisk_secrets.sqlite3 (see
# README.md and backend/app/core/secret_store.py).
#
#   scripts/keys.sh list              show every key's name and state
#   scripts/keys.sh set <name>        prompt for a value (hidden, no echo)
#   scripts/keys.sh remove <name>     delete a stored value
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR/backend"

export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed — see README.md for setup."
  exit 1
fi

exec uv run python -m app.cli.keys "$@"
