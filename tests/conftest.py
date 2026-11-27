"""Shared fixtures. Tests marked `database` need a PostgreSQL server.

Set `TOPOS_TEST_DSN` to a connection string for a role that may create databases. Each test that
needs one gets a fresh database, created from `template0` and dropped afterwards. CI sets
`TOPOS_REQUIRE_DATABASE=1`, so there a missing server fails the run instead of skipping it.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _admin_dsn() -> str:
    dsn = os.environ.get("TOPOS_TEST_DSN")
    if dsn:
        return dsn
    if os.environ.get("TOPOS_REQUIRE_DATABASE") == "1":
        pytest.fail("TOPOS_REQUIRE_DATABASE=1 but TOPOS_TEST_DSN is not set")
    pytest.skip("database tests need TOPOS_TEST_DSN")
    return ""


@pytest.fixture
def database() -> Iterator[Callable[[str], str]]:
    """Create fresh databases from SQL text, returning each one's connection string."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    admin = _admin_dsn()
    created: list[str] = []

    def create(sql: str) -> str:
        name = f"topos_test_{secrets.token_hex(6)}"
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
        created.append(name)
        params = conninfo_to_dict(admin)
        params["dbname"] = name
        dsn = make_conninfo(**params)
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql)
        return dsn

    yield create
    with psycopg.connect(admin, autocommit=True) as connection:
        for name in created:
            connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
