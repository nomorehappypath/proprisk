"""Security contract checks (SPEC.md chapter 7): a request from a foreign
page can't change anything, a planted secret never reaches a log line or an
error event, and no secret-shaped string is sitting in the delivered
repository."""

import re
from pathlib import Path

from app.config.settings import settings
from app.core import secret_store
from app.core.error_sink import report_error

REPO_ROOT = Path(__file__).resolve().parents[2]

# Deliberately narrow: a real key/token value, not the word "key" or a
# variable name. Matches common provider key shapes and generic
# `key = "<long random-looking string>"` assignments.
SECRET_SHAPED_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),  # OpenAI-style
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),  # Google API key
    re.compile(r"BSA[0-9A-Za-z_-]{20,}"),  # Brave Search key shape
]
ALLOWED_PATH_SUBSTRINGS = (".git/", "/data/", "node_modules/", "/.venv/", "/dist/")


def test_foreign_origin_refused(client):
    resp = client.put(
        "/api/settings/keys/census",
        json={"value": "a" * 20},
        headers={"Origin": "https://unrelated.invalid"},
    )
    assert resp.status_code == 403
    assert "error" in resp.json()


def test_foreign_host_refused(client):
    resp = client.put(
        "/api/settings/keys/census",
        json={"value": "a" * 20},
        headers={"Host": "unrelated.invalid"},
    )
    assert resp.status_code == 400


def test_removed_endpoints_are_truly_gone(client):
    for method, path in [
        ("POST", "/api/auth/login"),
        ("GET", "/api/auth/login"),
        ("GET", "/api/properties"),
        ("POST", "/api/checks/bulk-void"),
    ]:
        resp = client.request(method, path)
        assert resp.status_code == 404, f"{method} {path} returned {resp.status_code}"


def test_secret_never_logged(tmp_path, monkeypatch):
    log_path = tmp_path / "errors.jsonl"
    monkeypatch.setattr(settings, "error_sink_file", str(log_path))
    planted = "sk-planted-test-secret-should-never-appear-9f8e7d"
    secret_store.set_secret("openai", planted)
    try:
        report_error(
            RuntimeError(f"failed while using key {planted}"),
            where="test.secret_logging",
            context={"detail": f"value={planted}"},
        )
        logged = log_path.read_text()
        assert planted not in logged
        assert "[redacted]" in logged
    finally:
        secret_store.delete_secret("openai")


def test_repository_secret_scan():
    offenders = []
    for path in REPO_ROOT.rglob("*"):
        rel_path = str(path.relative_to(REPO_ROOT))
        if any(part in rel_path for part in ALLOWED_PATH_SUBSTRINGS):
            continue
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in SECRET_SHAPED_PATTERNS:
            if pattern.search(text):
                offenders.append(rel_path)
                break
    assert not offenders, f"secret-shaped string found in: {offenders}"
