"""Test harness — Postgres only (proprisk_test). SQLite is forbidden.

If DATABASE_URL is not already set, this module starts its own throwaway
PostgreSQL cluster on a free port (so `uv run pytest` works unattended on a
clean checkout, the way the board's own test runner invokes it) and stops
it when the test process exits. Set DATABASE_URL yourself to point at an
existing proprisk_test* database instead.
"""

import atexit
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path


def _with_postgres_on_path() -> dict:
    env = dict(os.environ)
    extra = ["/opt/homebrew/bin", "/usr/local/bin"]
    for prefix in sorted(__import__("glob").glob("/opt/homebrew/opt/postgresql@*")):
        extra.append(os.path.join(prefix, "bin"))
    env["PATH"] = ":".join(extra) + ":" + env.get("PATH", "")
    # A shell with no locale set can make Homebrew's PostgreSQL fail to
    # start ("postmaster became multithreaded during startup") on macOS.
    env["LC_ALL"] = env.get("LC_ALL") or "en_US.UTF-8"
    return env


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_ephemeral_postgres() -> str:
    """Creates and starts a throwaway cluster, registers its shutdown, and
    returns a DATABASE_URL pointing at a fresh `proprisk_test` database."""
    env = _with_postgres_on_path()
    port = _free_port()
    pg_dir = tempfile.mkdtemp(prefix="proprisk_conftest_pg_")
    sock_dir = tempfile.mkdtemp(prefix="proprisk_conftest_sock_")
    subprocess.run(
        ["initdb", "-D", pg_dir, "-U", "proprisk", "--auth=trust"],
        check=True, capture_output=True, text=True, env=env,
    )
    subprocess.run(
        ["pg_ctl", "-D", pg_dir, "-l", os.path.join(pg_dir, "postgres.log"),
         "-o", f"-p {port} -k {sock_dir} -c listen_addresses=127.0.0.1",
         "start", "-w"],
        check=True, capture_output=True, text=True, env=env,
    )
    subprocess.run(
        ["createdb", "-h", "127.0.0.1", "-p", str(port), "-U", "proprisk", "proprisk_test"],
        check=True, capture_output=True, text=True, env=env,
    )

    def _stop() -> None:
        subprocess.run(
            ["pg_ctl", "-D", pg_dir, "stop", "-m", "fast"],
            check=False, capture_output=True, env=env,
        )
        shutil.rmtree(pg_dir, ignore_errors=True)
        shutil.rmtree(sock_dir, ignore_errors=True)

    atexit.register(_stop)
    return f"postgresql+psycopg://proprisk@127.0.0.1:{port}/proprisk_test"


if "DATABASE_URL" not in os.environ:
    os.environ["DATABASE_URL"] = _start_ephemeral_postgres()

# The URL must still name a proprisk_test* database — asserted before any DDL.
assert "proprisk_test" in os.environ["DATABASE_URL"], (
    "tests must run on a proprisk_test* Postgres database"
)
os.environ.setdefault("DATA_DIR", "test_data")
_DATA_DIR = os.environ["DATA_DIR"]

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config.settings import settings
from app.core.local_identity import LOCAL_TENANT_ID, LOCAL_USER_ID
from app.core.secret_store import ALLOWED_NAMES, delete_secret
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Tenant, User

# The real secret store and error log live under PropRisk/data — tests must
# never read or write that real, permanent location (and a sandboxed runner
# may not even have permission to create it). Point both at a throwaway
# directory for the whole test session instead.
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="proprisk_conftest_data_"))
settings.secrets_db_path = str(_TEST_DATA_DIR / "proprisk_secrets.sqlite3")
settings.error_sink_file = str(_TEST_DATA_DIR / "errors.jsonl")
atexit.register(shutil.rmtree, _TEST_DATA_DIR, ignore_errors=True)

TABLES = ["audit_log", "ai_usage", "site_check_provider_usage", "site_check_coverage_gaps",
          "site_check_source_cache", "site_check_runs", "jobs", "users", "tenants"]


@pytest.fixture(scope="session", autouse=True)
def _darwin_subprocesses_use_posix_spawn():
    """Keep PROJ's unsafe at-fork cleanup out of every test subprocess.

    CPython's macOS posix_spawn fast path requires an absolute executable,
    ``close_fds=False``, and no ``cwd``.  Test subprocesses need all three:
    Poppler uses a PATH command and Alembic uses ``cwd``.  Normalize those
    arguments for the whole session and fail immediately if a test introduces
    an option that can only run through fork+exec.
    """
    if sys.platform != "darwin":
        yield
        return

    original_init = subprocess.Popen.__init__

    def spawn_only_init(self, args, *popen_args, **popen_kwargs):
        if popen_args:
            raise RuntimeError(
                "test subprocess options must be keyword arguments so the "
                "Darwin posix_spawn guard can validate them"
            )

        forbidden = {
            "preexec_fn": popen_kwargs.get("preexec_fn"),
            "start_new_session": popen_kwargs.get("start_new_session", False),
            "pass_fds": popen_kwargs.get("pass_fds", ()),
            "user": popen_kwargs.get("user"),
            "group": popen_kwargs.get("group"),
            "extra_groups": popen_kwargs.get("extra_groups"),
        }
        enabled = [name for name, value in forbidden.items() if value]
        if popen_kwargs.get("umask", -1) not in (-1, None):
            enabled.append("umask")
        if popen_kwargs.get("process_group") not in (None, -1):
            enabled.append("process_group")
        if enabled:
            raise RuntimeError(
                "fork-only subprocess options are forbidden in the macOS "
                f"test session: {', '.join(enabled)}"
            )

        shell = bool(popen_kwargs.get("shell", False))
        cwd = popen_kwargs.pop("cwd", None)
        env = popen_kwargs.get("env") or os.environ
        if cwd is not None:
            if shell:
                if not isinstance(args, (str, bytes)):
                    raise RuntimeError(
                        "shell=True with cwd requires a string command in tests"
                    )
                command = args.decode() if isinstance(args, bytes) else args
                args = ["/usr/bin/env", "-C", os.fspath(cwd), "/bin/sh", "-c", command]
                popen_kwargs["shell"] = False
            else:
                command = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
                args = ["/usr/bin/env", "-C", os.fspath(cwd), *command]
            popen_kwargs["executable"] = None
        elif not shell:
            command = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
            executable = popen_kwargs.get("executable") or command[0]
            executable_path = os.fsdecode(executable)
            if not os.path.dirname(executable_path):
                resolved = shutil.which(executable_path, path=env.get("PATH"))
                if resolved is None:
                    raise FileNotFoundError(executable_path)
                if popen_kwargs.get("executable") is not None:
                    popen_kwargs["executable"] = resolved
                else:
                    command[0] = resolved
                    args = command

        popen_kwargs["close_fds"] = False
        return original_init(self, args, **popen_kwargs)

    subprocess.Popen.__init__ = spawn_only_init
    try:
        yield
    finally:
        subprocess.Popen.__init__ = original_init


@pytest.fixture(scope="session", autouse=True)
def _schema():
    assert "proprisk_test" in str(engine.url), "tests must run on proprisk_test (Postgres)"
    # Self-heal: a previously KILLED run can leave sessions holding row locks
    # (brick 9 endpoints take FOR UPDATE), which wedges TRUNCATE/DDL forever.
    with engine.connect() as conn:
        conn.execute(text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        ))
        conn.commit()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    shutil.rmtree(_DATA_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def _clean_tables():
    yield
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} CASCADE"))
    shutil.rmtree(_DATA_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def _clean_secrets():
    yield
    for name in ALLOWED_NAMES:
        delete_secret(name)


@pytest.fixture(autouse=True)
def _local_identity():
    """Every request PropRisk serves resolves to this one fixed office and
    owner (app/api/deps.py) — the baseline migration inserts these rows for
    real; here, insert them fresh before each test, since _clean_tables
    truncates everything (including tenants/users) after the previous one."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, name, monthly_budget_cents, created_at) "
                "VALUES (:id, 'Local', 2000, now()) ON CONFLICT (id) DO NOTHING"
            ),
            {"id": LOCAL_TENANT_ID},
        )
        conn.execute(
            text(
                "INSERT INTO users (id, tenant_id, display_name, role, created_at) "
                "VALUES (:id, :tenant_id, 'Local owner', 'owner', now()) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": LOCAL_USER_ID, "tenant_id": LOCAL_TENANT_ID},
        )
    yield


@pytest.fixture
def local_tenant_user(db):
    """The fixed office/owner every HTTP request actually resolves to —
    for tests whose primary flow goes through the API as "the" tenant."""
    return db.get(Tenant, LOCAL_TENANT_ID), db.get(User, LOCAL_USER_ID)


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    # An open read transaction here can queue behind the teardown TRUNCATE
    # once endpoints take row locks (brick 9) — end it explicitly.
    session.rollback()
    session.close()


@pytest.fixture
def client():
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def make_tenant(db):
    def _make(name: str, budget_cents: int | None = None) -> Tenant:
        tenant = Tenant(name=name, monthly_budget_cents=budget_cents)
        db.add(tenant)
        db.flush()
        db.commit()
        return tenant

    return _make


@pytest.fixture
def make_user(db):
    def _make(tenant: Tenant, label: str, password: str = "unused") -> User:
        display_name = label.split("@")[0].title() if "@" in label else label
        user = User(
            tenant_id=tenant.id,
            display_name=display_name,
        )
        db.add(user)
        db.commit()
        return user

    return _make


@pytest.fixture
def login(client):
    def _login(*args, **kwargs) -> dict:
        return {}

    return _login
