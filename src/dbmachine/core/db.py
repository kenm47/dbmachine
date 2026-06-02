"""Database connectivity and schema application.

Engines are created per-project from the persisted connection config. Migrations
use **Alembic's** metadata comparison (``compare_metadata`` / ``produce_migrations``)
against the compiler's :class:`MetaData` — we deliberately do not hand-roll a
declarative diff engine. Destructive operations (drop table/column) are detected
and require explicit confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from .compiler import build_metadata
from .config import Project
from .spec import Spec
from .sqlsplit import split_sql

_DESTRUCTIVE = {"remove_table", "remove_column"}


def _include_object(obj, name, type_, reflected, compare_to):
    """Keep Alembic from managing dbmachine's own out-of-band objects.

    The audit/actions tables, triggers, views and RLS are created via idempotent
    extra DDL — they are not in the target MetaData, so without this filter
    Alembic would propose dropping them on every migrate.
    """
    if name and name.startswith("dbm_"):
        return False
    return True


def _mc_opts(md) -> dict:
    return {
        "compare_type": True,
        "target_metadata": md,
        "include_object": _include_object,
    }


# Test seam: when set, engine_for ignores project config and uses this URL.
# Production code never sets it; integration tests point it at an embedded PG.
_URL_OVERRIDE: str | None = None


def engine_for(proj: Project) -> Engine:
    url = _URL_OVERRIDE or proj.config.url()
    return sa.create_engine(url, future=True)


def can_connect(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        return True
    except Exception:
        return False


def _db_is_empty(conn) -> bool:
    n = conn.execute(
        sa.text(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name NOT LIKE 'dbm\\_%'"
        )
    ).scalar()
    return (n or 0) == 0


def _exec_script(conn, script: str | list[str]) -> None:
    """Execute a SQL script (or list of scripts), splitting dollar-quote-safely."""
    scripts = script if isinstance(script, list) else [script]
    for chunk in scripts:
        for stmt in split_sql(chunk):
            conn.execute(sa.text(stmt))


@dataclass
class MigrationPlan:
    fresh: bool
    changes: list[str] = field(default_factory=list)
    destructive: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def has_changes(self) -> bool:
        return self.fresh or bool(self.changes)


def _describe_diff(diff) -> tuple[str, bool]:
    """Turn an Alembic diff tuple into (human description, is_destructive)."""
    # diffs are tuples or lists-of-tuples (for modify_*)
    if isinstance(diff, list):
        descs = [_describe_diff(d) for d in diff]
        return ("; ".join(d for d, _ in descs), any(x for _, x in descs))
    op = diff[0]
    destructive = op in _DESTRUCTIVE
    if op == "add_table":
        return (f"create table {diff[1].name}", destructive)
    if op == "remove_table":
        return (f"DROP TABLE {diff[1].name}", destructive)
    if op == "add_column":
        return (f"add column {diff[2]}.{diff[3].name}", destructive)
    if op == "remove_column":
        return (f"DROP COLUMN {diff[2]}.{diff[3].name}", destructive)
    if op.startswith("add_"):
        return (f"{op} on {diff[1] if len(diff) > 1 else '?'}", destructive)
    if op.startswith("remove_"):
        return (f"{op}", True)
    return (op, destructive)


def plan_migration(proj: Project, spec: Spec) -> tuple[MigrationPlan, object, object]:
    """Compute a migration plan against the live DB. Returns (plan, md, extra)."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    md, extra = build_metadata(spec)
    engine = engine_for(proj)
    with engine.connect() as conn:
        if _db_is_empty(conn):
            return MigrationPlan(fresh=True, changes=["create entire schema"]), md, extra
        mc = MigrationContext.configure(conn, opts=_mc_opts(md))
        diffs = compare_metadata(mc, md)
    plan = MigrationPlan(fresh=False)
    for d in diffs:
        desc, destructive = _describe_diff(d)
        plan.changes.append(desc)
        if destructive:
            plan.destructive.append(desc)
    return plan, md, extra


def apply_migration(proj: Project, spec: Spec, *, confirm_destructive: bool = False) -> MigrationPlan:
    """Apply the migration. Raises if destructive changes are unconfirmed."""
    from alembic.autogenerate import produce_migrations
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.operations import ops as alops

    from .compiler import render_schema_sql

    plan, md, extra = plan_migration(proj, spec)
    if plan.destructive and not confirm_destructive:
        raise PermissionError(
            "migration contains destructive changes; re-run with --confirm:\n  - "
            + "\n  - ".join(plan.destructive)
        )

    engine = engine_for(proj)
    with engine.begin() as conn:
        if plan.fresh:
            # Fresh DB: run the full rendered schema directly (simplest, reliable).
            _exec_script(conn, render_schema_sql(spec))
        else:
            # Extensions + enum types must exist BEFORE Alembic adds any column
            # that references them (e.g. a newly added enum-typed field).
            _exec_script(conn, list(extra.preamble))

            mc = MigrationContext.configure(conn, opts=_mc_opts(md))
            migration = produce_migrations(mc, md)
            operations = Operations(mc)

            def _run(container) -> None:
                for op in container.ops:
                    if isinstance(op, alops.OpContainer):
                        _run(op)
                    else:
                        operations.invoke(op)

            _run(migration.upgrade_ops)
            # Re-apply the remaining idempotent extra DDL (triggers/views/RLS).
            _exec_script(conn, extra.functions)
            _exec_script(conn, extra.triggers)
            _exec_script(conn, extra.views)
            _exec_script(conn, extra.rls)
            _exec_script(conn, list(extra.seeds))
    plan.applied = True
    return plan
