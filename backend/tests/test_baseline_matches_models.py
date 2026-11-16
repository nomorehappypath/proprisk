"""Checks that a database built fresh from the baseline Alembic migration
has exactly the tables and columns app/models.py declares — so the one
migration file and the models can never silently drift apart.

Runs `alembic upgrade head` as its own subprocess (the way scripts/launch.sh
runs it) against a throwaway database, because `app/config/settings.py`
caches its `DATABASE_URL` at import time — only a fresh process picks up a
different one.
"""

import os
import subprocess
import sys
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import inspect

from app.db import Base, engine

BACKEND_DIR = Path(__file__).resolve().parents[1]
CHECK_DB = "proprisk_baseline_check"


def _admin_connection():
    return engine.execution_options(isolation_level="AUTOCOMMIT").connect()


def test_baseline_matches_models():
    base_url = str(engine.url).rsplit("/", 1)[0]
    check_url = f"{base_url}/{CHECK_DB}"

    with _admin_connection() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{CHECK_DB}"'))
        conn.execute(sa.text(f'CREATE DATABASE "{CHECK_DB}"'))

    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND_DIR,
            env={**os.environ, "DATABASE_URL": check_url},
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr

        check_engine = sa.create_engine(check_url)
        try:
            inspector = inspect(check_engine)
            migrated_tables = set(inspector.get_table_names()) - {"alembic_version"}
            model_tables = set(Base.metadata.tables.keys())
            assert migrated_tables == model_tables, (
                f"migration vs models table mismatch: "
                f"only in migration={migrated_tables - model_tables}, "
                f"only in models={model_tables - migrated_tables}"
            )

            for table_name, table in Base.metadata.tables.items():
                migrated_columns = {c["name"] for c in inspector.get_columns(table_name)}
                model_columns = {c.name for c in table.columns}
                assert migrated_columns == model_columns, (
                    f"{table_name}: migration vs model column mismatch: "
                    f"only in migration={migrated_columns - model_columns}, "
                    f"only in models={model_columns - migrated_columns}"
                )

            with check_engine.connect() as conn:
                tenants = conn.execute(sa.text("SELECT count(*) FROM tenants")).scalar_one()
                users = conn.execute(sa.text("SELECT count(*) FROM users")).scalar_one()
                assert tenants == 1, "the baseline migration must insert exactly one office row"
                assert users == 1, "the baseline migration must insert exactly one owner row"
        finally:
            check_engine.dispose()
    finally:
        with _admin_connection() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{CHECK_DB}"'))
