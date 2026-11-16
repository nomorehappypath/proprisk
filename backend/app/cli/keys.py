"""Command-line access to the local secret store, for `scripts/keys.sh`.
Takes one subcommand (list | set | remove) and prints one line per result;
`set` reads the value from stdin without echoing it, so it never appears in
shell history or a process listing.

    uv run python -m app.cli.keys list
    uv run python -m app.cli.keys set <name>
    uv run python -m app.cli.keys remove <name>
"""

import getpass
import sys

from app.core import secret_store


def _list() -> int:
    for row in secret_store.list_secrets():
        state = f"set (…{row['last4']}, saved {row['updated_at']})" if row["is_set"] else "not set"
        print(f"{row['name']}: {state}")
    return 0


def _set(name: str) -> int:
    if name not in secret_store.ALLOWED_NAMES:
        print(f"Unknown key name: {name}", file=sys.stderr)
        return 1
    value = getpass.getpass(f"Value for {name} (hidden, not echoed): ").strip()
    if not value:
        print("Nothing entered — not saved.", file=sys.stderr)
        return 1
    secret_store.set_secret(name, value)
    print(f"{name}: saved.")
    return 0


def _remove(name: str) -> int:
    if name not in secret_store.ALLOWED_NAMES:
        print(f"Unknown key name: {name}", file=sys.stderr)
        return 1
    removed = secret_store.delete_secret(name)
    print(f"{name}: {'removed.' if removed else 'was not set.'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] not in ("list", "set", "remove"):
        print("Usage: keys.py list | set <name> | remove <name>", file=sys.stderr)
        return 1
    command = argv[0]
    if command == "list":
        return _list()
    if len(argv) < 2:
        print(f"Usage: keys.py {command} <name>", file=sys.stderr)
        return 1
    return _set(argv[1]) if command == "set" else _remove(argv[1])


if __name__ == "__main__":
    raise SystemExit(main())
