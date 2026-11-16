"""The one place every PropRisk API key and secret lives: a single SQLite
file (`PropRisk/data/proprisk_secrets.sqlite3`, created with 0600 permissions
inside a 0700 folder on first use). No environment variable or .env file is
ever read for a secret — this module is the only source.

Each call opens its own connection, does its one thing, and closes it: safe
to call from both the server and the worker process, since SQLite handles
the file locking between them.
"""

import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from pydantic import SecretStr

from app.config.settings import settings

# The fixed set of names this store will hold. `contact_email` is not a
# secret (it is shown back in full), but it lives in the same small store so
# there is only one place that needs the 0600 file.
ALLOWED_NAMES = frozenset(
    {
        "openai",
        "arcgis",
        "census",
        "google_maps",
        "brave_search",
        "bing_search",
        "error_sink_token",
        "contact_email",
    }
)


class UnknownSecretName(Exception):
    """Raised when a caller asks for a name outside ALLOWED_NAMES."""


class SecretRow(TypedDict):
    name: str
    is_set: bool
    last4: str | None
    updated_at: str | None


def _require_known(name: str) -> None:
    if name not in ALLOWED_NAMES:
        raise UnknownSecretName(name)


def _db_path() -> Path:
    return Path(settings.secrets_db_path)


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, stat.S_IRWXU)  # 0700 — owner only
    is_new = not path.exists()
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS secrets ("
        "name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.commit()
    if is_new:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600 — owner read/write only
    return conn


def get_secret(name: str) -> SecretStr | None:
    _require_known(name)
    conn = _connect()
    try:
        row = conn.execute("SELECT value FROM secrets WHERE name = ?", (name,)).fetchone()
    finally:
        conn.close()
    return SecretStr(row[0]) if row else None


def set_secret(name: str, value: str) -> None:
    _require_known(name)
    now = datetime.now(UTC).isoformat()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO secrets (name, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (name, value, now),
        )
        conn.commit()
    finally:
        conn.close()


def delete_secret(name: str) -> bool:
    _require_known(name)
    conn = _connect()
    try:
        cursor = conn.execute("DELETE FROM secrets WHERE name = ?", (name,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def list_secrets() -> list[SecretRow]:
    conn = _connect()
    try:
        rows = {
            name: (value, updated_at)
            for name, value, updated_at in conn.execute("SELECT name, value, updated_at FROM secrets")
        }
    finally:
        conn.close()
    result: list[SecretRow] = []
    for name in sorted(ALLOWED_NAMES):
        found = rows.get(name)
        if found is None:
            result.append({"name": name, "is_set": False, "last4": None, "updated_at": None})
        else:
            value, updated_at = found
            result.append(
                {
                    "name": name,
                    "is_set": True,
                    "last4": value[-4:] if len(value) >= 4 else value,
                    "updated_at": updated_at,
                }
            )
    return result
