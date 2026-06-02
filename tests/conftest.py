"""Test fixtures.

Unit tests need no database. Integration tests (marked ``@pytest.mark.postgres``)
run against an embedded Postgres provided by ``pgserver`` when available; if it
is not installed they are skipped, so the suite stays green anywhere.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def bookings_spec():
    from dbmachine.core.spec import load_spec

    return load_spec(str(EXAMPLES / "bookings" / "app.dbm.yaml"))


@pytest.fixture
def crm_spec():
    from dbmachine.core.spec import load_spec

    return load_spec(str(EXAMPLES / "crm" / "app.dbm.yaml"))


# --- embedded Postgres -----------------------------------------------------

try:
    import pgserver  # noqa: F401

    _HAVE_PG = True
except Exception:  # pragma: no cover
    _HAVE_PG = False

# CI sets this to a real postgres:16 (which includes btree_gist) so the
# exclusion-constraint invariant test actually runs. Locally we fall back to the
# embedded pgserver (no contrib → that one test skips).
_EXTERNAL_DSN = os.environ.get("DBMACHINE_TEST_DSN")


class _ExternalServer:
    """Adapter exposing the pgserver surface (.get_uri/.psql) over a real DB."""

    def __init__(self, dsn: str):
        self._dsn = dsn

    def get_uri(self) -> str:
        return self._dsn

    def _conn(self):
        import psycopg

        url = self._dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        c = psycopg.connect(url)
        c.autocommit = True
        return c

    def psql(self, sql: str) -> str:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(sql)
            if cur.description is None:
                return ""
            rows = cur.fetchall()
            body = "\n".join(" ".join(str(v) for v in r) for r in rows)
            return f"{body}\n({len(rows)} rows)"

    def cleanup(self):
        pass


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory):
    if _EXTERNAL_DSN:
        yield _ExternalServer(_EXTERNAL_DSN)
        return
    if not _HAVE_PG:
        pytest.skip("no Postgres available (set DBMACHINE_TEST_DSN or install pgserver)")
    import pgserver

    data_dir = tmp_path_factory.mktemp("pgdata")
    srv = pgserver.get_server(str(data_dir))
    yield srv
    srv.cleanup()


def _psycopg_url(srv) -> str:
    uri = srv.get_uri()
    # route through psycopg v3 if a v2-style URL is given
    if uri.startswith("postgresql+psycopg://"):
        return uri
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


def _has_extension(srv, name: str) -> bool:
    out = srv.psql(
        f"SELECT 1 FROM pg_available_extensions WHERE name = '{name}';"
    )
    return "1" in out and "(0 rows)" not in out


@pytest.fixture(scope="session")
def has_btree_gist(pg_server):
    return _has_extension(pg_server, "btree_gist")


def _make_project(tmp_path, pg_server, name, spec):
    from dbmachine.core import db
    from dbmachine.core.config import Project

    # fresh schema per test (drops everything, incl. dbm_* tables)
    pg_server.psql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")

    src = EXAMPLES / name
    root = tmp_path / name
    shutil.copytree(src, root)
    proj = Project.create(root, name)
    db._URL_OVERRIDE = _psycopg_url(pg_server)
    return proj, spec


@pytest.fixture
def bookings_project(tmp_path, pg_server, bookings_spec, has_btree_gist):
    """A bookings project on a fresh DB. Needs btree_gist for the exclusion constraint."""
    from dbmachine.core import db

    if not has_btree_gist:
        pytest.skip("btree_gist extension unavailable (needed for exclusion constraint)")
    try:
        yield _make_project(tmp_path, pg_server, "bookings", bookings_spec)
    finally:
        db._URL_OVERRIDE = None


@pytest.fixture
def crm_project(tmp_path, pg_server, crm_spec):
    """A CRM project on a fresh DB — no Postgres contrib extensions required."""
    from dbmachine.core import db

    try:
        yield _make_project(tmp_path, pg_server, "crm", crm_spec)
    finally:
        db._URL_OVERRIDE = None
