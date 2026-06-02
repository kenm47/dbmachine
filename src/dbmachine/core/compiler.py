"""The compiler: spec → Postgres backend.

Produces two artifacts from a :class:`~dbmachine.core.spec.Spec`:

1. A SQLAlchemy :class:`MetaData` describing tables, columns, FKs, checks,
   unique constraints, indexes and exclusion constraints. This is the canonical
   table-level schema and the diff target for migrations (Alembic compares the
   live DB against it — we deliberately do **not** hand-roll a declarative diff
   engine).
2. :class:`ExtraDDL` — the objects SQLAlchemy metadata reflection doesn't model
   well (the ``updated_at`` trigger, the audit-log table + trigger, views, RLS
   roles/policies, seed inserts). These are managed idempotently on each apply.

All identifiers in the spec are validated as SQL-safe before reaching here, so
rendering is purely mechanical.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.schema import CreateIndex, CreateTable

from .spec import EntityDef, FieldDef, FieldType, Spec

# Logical type → SQLAlchemy type factory.
_TYPE_MAP = {
    FieldType.text: lambda: sa.Text(),
    FieldType.integer: lambda: sa.Integer(),
    FieldType.bigint: lambda: sa.BigInteger(),
    FieldType.numeric: lambda: sa.Numeric(),
    FieldType.boolean: lambda: sa.Boolean(),
    FieldType.timestamptz: lambda: pg.TIMESTAMP(timezone=True),
    FieldType.date: lambda: sa.Date(),
    FieldType.time: lambda: sa.Time(),
    FieldType.uuid: lambda: pg.UUID(as_uuid=False),
    FieldType.jsonb: lambda: pg.JSONB(),
}

_ON_DELETE = {"cascade": "CASCADE", "restrict": "RESTRICT", "set_null": "SET NULL"}


@dataclass
class ExtraDDL:
    """DDL beyond what SQLAlchemy metadata renders, applied idempotently."""

    preamble: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)  # CREATE OR REPLACE FUNCTION
    triggers: list[str] = field(default_factory=list)  # drop-if-exists + create
    views: list[str] = field(default_factory=list)  # CREATE OR REPLACE VIEW
    rls: list[str] = field(default_factory=list)
    seeds: list[str] = field(default_factory=list)

    def all_statements(self) -> list[str]:
        return [
            *self.preamble,
            *self.functions,
            *self.triggers,
            *self.views,
            *self.rls,
        ]


def _pg_enum(name: str, values: list[str]) -> pg.ENUM:
    # create_type=False: the TYPE is created explicitly via a guarded DO block in
    # the preamble, so neither CreateTable nor Alembic should emit it.
    return pg.ENUM(*values, name=name, create_type=False)


def _sql_default(fld: FieldDef):
    if fld.default is None:
        return None
    return sa.text(_literal(fld.default))


def _literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    # treat as a string literal
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def build_metadata(spec: Spec) -> tuple[sa.MetaData, ExtraDDL]:
    md = sa.MetaData()
    extra = ExtraDDL()
    # btree_gist is only needed for exclusion constraints that combine equality
    # (=) with range overlap (&&) — emit it only when the spec actually uses one.
    if any(e.exclusions for e in spec.entities.values()):
        extra.preamble.append("CREATE EXTENSION IF NOT EXISTS btree_gist;")

    # Shared ENUM type objects keyed by name so the same TYPE is reused.
    # We render CREATE TYPE explicitly (SQLAlchemy only emits it during
    # create_all, not when compiling CreateTable by hand) and wrap each in a
    # guarded DO block so re-applying is idempotent. create_type=False stops
    # SQLAlchemy/Alembic from also trying to emit it.
    enum_types: dict[str, pg.ENUM] = {
        name: _pg_enum(name, e.values) for name, e in spec.enums.items()
    }
    for name, e in spec.enums.items():
        vals = ", ".join(_literal(v) for v in e.values)
        extra.preamble.append(
            f"DO $$ BEGIN CREATE TYPE {name} AS ENUM ({vals}); "
            f"EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
        )

    tables: dict[str, sa.Table] = {}
    for ename, entity in spec.entities.items():
        tables[ename] = _build_table(md, ename, entity, spec, enum_types)

    # updated_at trigger function (shared) + per-table triggers.
    extra.functions.append(_TOUCH_FN)
    for ename in spec.entities:
        extra.triggers.append(_touch_trigger(ename))

    # audit log infrastructure
    from .actions import ACTIONS_LOG_DDL

    extra.functions.append(ACTIONS_LOG_DDL)
    extra.functions.append(_AUDIT_FN)
    for ename in spec.entities:
        extra.triggers.append(_audit_trigger(ename))

    # views
    for vname, view in spec.views.items():
        extra.views.append(f"CREATE OR REPLACE VIEW {vname} AS {view.sql.rstrip(';')};")

    # RLS roles + policies
    extra.rls.extend(_render_rls(spec))

    # seeds
    for ename, rows in spec.seeds.items():
        extra.seeds.extend(_render_seeds(ename, spec.entities[ename], spec, rows))

    return md, extra


def _build_table(
    md: sa.MetaData,
    ename: str,
    entity: EntityDef,
    spec: Spec,
    enum_types: dict[str, pg.ENUM],
) -> sa.Table:
    cols: list[sa.Column] = [
        sa.Column(
            "id",
            pg.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        )
    ]

    for fname, fld in entity.fields.items():
        if fld.type is FieldType.enum:
            coltype = enum_types[fld.enum]
        else:
            coltype = _TYPE_MAP[fld.type]()
        cols.append(
            sa.Column(
                fname,
                coltype,
                nullable=not fld.required,
                unique=fld.unique,
                server_default=_sql_default(fld),
            )
        )

    # belongs_to → <rel>_id FK column
    fks: list[sa.ForeignKeyConstraint] = []
    for rname, rel in entity.relationships.items():
        if rel.kind != "belongs_to":
            continue  # has_many is the inverse; m2m handled via join table below
        col = f"{rname}_id"
        cols.append(
            sa.Column(col, pg.UUID(as_uuid=False), nullable=not rel.required)
        )
        fks.append(
            sa.ForeignKeyConstraint(
                [col],
                [f"{rel.target}.id"],
                name=f"fk_{ename}_{rname}",
                ondelete=_ON_DELETE[rel.on_delete],
            )
        )

    cols += [
        sa.Column("created_at", pg.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", pg.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]

    constraints: list = list(fks)
    for chk in entity.checks:
        constraints.append(sa.CheckConstraint(chk.expr, name=chk.name))
    for uc in entity.unique:
        constraints.append(sa.UniqueConstraint(*uc.fields, name=uc.name))
    for exc in entity.exclusions:
        elements = []
        for el in exc.with_:
            target = el.field if el.field else sa.text(el.expr)
            elements.append((target, el.op))
        kwargs = {"name": exc.name, "using": exc.using}
        if exc.where:
            kwargs["where"] = sa.text(exc.where)
        constraints.append(ExcludeConstraint(*elements, **kwargs))

    table = sa.Table(ename, md, *cols, *constraints)

    for idx in entity.indexes:
        sa.Index(idx.name, *[table.c[f] for f in idx.fields], unique=idx.unique)

    # many_to_many → association table
    for rname, rel in entity.relationships.items():
        if rel.kind != "many_to_many":
            continue
        _build_m2m(md, ename, rel.target)

    return table


def _build_m2m(md: sa.MetaData, a: str, b: str) -> None:
    name = "_".join(sorted([a, b])) + "_link"
    if name in md.tables:
        return
    sa.Table(
        name,
        md,
        sa.Column(f"{a}_id", pg.UUID(as_uuid=False), sa.ForeignKey(f"{a}.id", ondelete="CASCADE"), primary_key=True),
        sa.Column(f"{b}_id", pg.UUID(as_uuid=False), sa.ForeignKey(f"{b}.id", ondelete="CASCADE"), primary_key=True),
    )


# ---------------------------------------------------------------------------
# Extra DDL bodies
# ---------------------------------------------------------------------------

_TOUCH_FN = """\
CREATE OR REPLACE FUNCTION dbm_touch_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;"""


def _touch_trigger(table: str) -> str:
    return (
        f"DROP TRIGGER IF EXISTS {table}_touch_updated_at ON {table};\n"
        f"CREATE TRIGGER {table}_touch_updated_at BEFORE UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION dbm_touch_updated_at();"
    )


# Audit log: a single table + a generic trigger. Note: bulk imports bypass this
# per-row trigger and write one summary row instead (see runtime.import_rows).
_AUDIT_FN = """\
CREATE TABLE IF NOT EXISTS dbm_audit_log (
  id bigserial PRIMARY KEY,
  at timestamptz NOT NULL DEFAULT now(),
  actor text NOT NULL DEFAULT current_user,
  entity text NOT NULL,
  action text NOT NULL,
  row_id text,
  changes jsonb
);
CREATE OR REPLACE FUNCTION dbm_audit() RETURNS trigger AS $$
DECLARE
  rid text;
  diff jsonb;
BEGIN
  IF current_setting('dbm.bulk', true) = 'on' THEN
    RETURN COALESCE(NEW, OLD);
  END IF;
  IF (TG_OP = 'DELETE') THEN
    rid := OLD.id::text;
    diff := to_jsonb(OLD);
  ELSIF (TG_OP = 'UPDATE') THEN
    rid := NEW.id::text;
    diff := jsonb_build_object('old', to_jsonb(OLD), 'new', to_jsonb(NEW));
  ELSE
    rid := NEW.id::text;
    diff := to_jsonb(NEW);
  END IF;
  INSERT INTO dbm_audit_log(entity, action, row_id, changes)
    VALUES (TG_TABLE_NAME, TG_OP, rid, diff);
  RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;"""


def _audit_trigger(table: str) -> str:
    return (
        f"DROP TRIGGER IF EXISTS {table}_audit ON {table};\n"
        f"CREATE TRIGGER {table}_audit AFTER INSERT OR UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION dbm_audit();"
    )


# ---------------------------------------------------------------------------
# RLS
# ---------------------------------------------------------------------------


def _render_rls(spec: Spec) -> list[str]:
    """Compile policies → Postgres RLS.

    v1 roles: ``operator`` (full) and ``agent`` (scoped). Per the spec, in v1
    RLS is for structural correctness / forward-compatibility, **not** a
    security boundary against a locally-credentialed agent.
    """
    out: list[str] = [
        "DO $$ BEGIN CREATE ROLE dbm_operator; EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
        "DO $$ BEGIN CREATE ROLE dbm_agent; EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
    ]
    agent_pol = spec.policies.get("agent")
    for ename in spec.entities:
        out.append(f"ALTER TABLE {ename} ENABLE ROW LEVEL SECURITY;")
        # operator: full access
        out.append(
            f"DROP POLICY IF EXISTS {ename}_operator ON {ename};"
        )
        out.append(
            f"CREATE POLICY {ename}_operator ON {ename} TO dbm_operator "
            f"USING (true) WITH CHECK (true);"
        )
        out.append(f"GRANT ALL ON {ename} TO dbm_operator;")

        # agent: per-policy grants (default: SELECT/INSERT/UPDATE, no DELETE)
        ep = agent_pol.entities.get(ename) if agent_pol else None
        sel = ep.select if ep else True
        ins = ep.insert if ep else True
        upd = ep.update if ep else True
        dele = ep.delete if ep else False
        out.append(f"DROP POLICY IF EXISTS {ename}_agent ON {ename};")
        out.append(
            f"CREATE POLICY {ename}_agent ON {ename} TO dbm_agent "
            f"USING (true) WITH CHECK (true);"
        )
        grants = [g for g, on in
                  [("SELECT", sel), ("INSERT", ins), ("UPDATE", upd), ("DELETE", dele)]
                  if on]
        out.append(f"REVOKE ALL ON {ename} FROM dbm_agent;")
        if grants:
            out.append(f"GRANT {', '.join(grants)} ON {ename} TO dbm_agent;")
    return out


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------


def _render_seeds(ename: str, entity: EntityDef, spec: Spec, rows: list[dict]) -> list[str]:
    stmts = []
    for row in rows:
        cols = list(row.keys())
        vals = [_literal(row[c]) for c in cols]
        col_sql = ", ".join(cols)
        val_sql = ", ".join(vals)
        stmts.append(
            f"INSERT INTO {ename} ({col_sql}) VALUES ({val_sql}) ON CONFLICT DO NOTHING;"
        )
    return stmts


# ---------------------------------------------------------------------------
# Rendering full DDL (used by `compile` and first-run migrate preview)
# ---------------------------------------------------------------------------


def render_schema_sql(spec: Spec) -> str:
    """Render the complete, ordered DDL for a fresh database.

    Pure string generation — no DB connection needed (the unit-test surface).
    """
    md, extra = build_metadata(spec)
    dialect = pg.dialect()
    parts: list[str] = ["-- Generated by dbmachine. Do not edit by hand.", ""]

    parts.append("-- Extensions")
    parts += extra.preamble
    parts.append("")

    parts.append("-- Tables")
    for table in md.sorted_tables:
        parts.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";")
        for idx in table.indexes:
            parts.append(str(CreateIndex(idx).compile(dialect=dialect)).strip() + ";")
    parts.append("")

    parts.append("-- Functions & triggers")
    parts += extra.functions
    parts += extra.triggers
    parts.append("")

    if extra.views:
        parts.append("-- Views")
        parts += extra.views
        parts.append("")

    parts.append("-- Row-Level Security")
    parts += extra.rls
    parts.append("")

    if extra.seeds:
        parts.append("-- Seeds")
        parts += extra.seeds
        parts.append("")

    return "\n".join(parts) + "\n"
