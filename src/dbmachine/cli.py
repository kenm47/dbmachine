"""dbmachine CLI — a thin Typer adapter over the structured-return core.

Every command emits machine-readable JSON on stdout so a coding agent can parse
results reliably. The CLI does no business logic itself: it parses arguments,
calls a core function that returns plain Python objects, and serialises the
result. This is what keeps a future MCP server a ~100-line drop-in over the same
core.
"""

from __future__ import annotations

import json
import sys
from typing import Optional

import typer

from .core import docker as docker_mod
from .core.config import Project, ProjectError
from .core.spec import SpecError, load_spec

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Turn a Postgres database into an agent-operable application backend.",
)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def emit(data, *, pretty: bool = False) -> None:
    indent = 2 if pretty else None
    typer.echo(json.dumps({"ok": True, "data": data}, indent=indent, default=str))


def fail(message: str, *, code: str = "error", exit_code: int = 1, **extra) -> None:
    typer.echo(
        json.dumps({"ok": False, "error": {"code": code, "message": message, **extra}}),
        err=False,
    )
    raise typer.Exit(exit_code)


def _project() -> Project:
    try:
        return Project.find()
    except ProjectError as e:
        fail(str(e), code="no_project")


def _spec(proj: Project):
    try:
        return load_spec(str(proj.spec_path))
    except SpecError as e:
        fail(str(e), code="invalid_spec")


def _runtime(proj: Project, spec):
    from .core.runtime import Runtime

    return Runtime(project=proj, spec=spec)


def _parse_json(raw: Optional[str], what: str = "payload") -> dict:
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"invalid JSON {what}: {e}", code="bad_json")
    if not isinstance(val, dict):
        fail(f"{what} must be a JSON object", code="bad_json")
    return val


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@app.command()
def init(
    directory: str = typer.Argument(".", help="Project directory to scaffold."),
    name: Optional[str] = typer.Option(None, "--name", help="App name (default: dir name)."),
):
    """Scaffold a new dbmachine project (spec + ops + docker-compose + AGENTS.md)."""
    from .core.scaffold import init_project

    result = init_project(directory, app_name=name)
    emit(result, pretty=True)


@app.command()
def up():
    """Start local Postgres via Docker, auto-detecting a free host port."""
    proj = _project()
    try:
        emit(docker_mod.up(proj))
    except RuntimeError as e:
        fail(str(e), code="docker")


@app.command()
def down(
    destroy: bool = typer.Option(False, "--destroy", help="Also delete the data volume."),
):
    """Stop local Postgres (optionally destroying its data)."""
    proj = _project()
    try:
        emit(docker_mod.down(proj, destroy=destroy))
    except RuntimeError as e:
        fail(str(e), code="docker")


@app.command()
def status():
    """Report whether local Postgres is running."""
    emit(docker_mod.status(_project()))


# ---------------------------------------------------------------------------
# Compile / migrate / docs
# ---------------------------------------------------------------------------


@app.command()
def compile(  # noqa: A001 - matches the documented command name
    out: Optional[str] = typer.Option(None, "--out", help="Write schema.sql to this path."),
):
    """Validate the spec and render the Postgres schema + data dictionary."""
    from .core.compiler import render_schema_sql
    from .core.docs import generate_data_dictionary

    proj = _project()
    spec = _spec(proj)
    sql = render_schema_sql(spec)
    proj.build_dir.mkdir(parents=True, exist_ok=True)
    schema_path = proj.build_dir / "schema.sql"
    schema_path.write_text(sql)
    (proj.build_dir / "data_dictionary.md").write_text(generate_data_dictionary(spec))
    out_path = out or str(schema_path)
    if out and out != str(schema_path):
        from pathlib import Path

        Path(out).write_text(sql)
    emit(
        {
            "app": spec.app.name,
            "entities": list(spec.entities.keys()),
            "schema_sql": out_path,
            "statements": sql.count(";"),
            "valid": True,
        }
    )


@app.command()
def migrate(
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview changes without applying."),
    confirm: bool = typer.Option(False, "--confirm", help="Confirm destructive changes."),
):
    """Diff the spec against the live DB and apply migrations (Alembic-backed)."""
    from .core import db

    proj = _project()
    spec = _spec(proj)
    engine = db.engine_for(proj)
    if not db.can_connect(engine):
        fail(
            "cannot connect to the database. Run `dbmachine up` first.",
            code="no_db",
        )
    try:
        plan, _, _ = db.plan_migration(proj, spec)
        if dry_run:
            emit(
                {
                    "dry_run": True,
                    "fresh": plan.fresh,
                    "changes": plan.changes,
                    "destructive": plan.destructive,
                    "has_changes": plan.has_changes,
                }
            )
            return
        if not plan.has_changes:
            emit({"applied": False, "changes": [], "message": "schema already up to date"})
            return
        applied = db.apply_migration(proj, spec, confirm_destructive=confirm)
        emit(
            {
                "applied": True,
                "fresh": applied.fresh,
                "changes": applied.changes,
                "destructive": applied.destructive,
            }
        )
    except PermissionError as e:
        fail(str(e), code="destructive_unconfirmed")
    except Exception as e:  # surface DB errors as structured output
        fail(f"migration failed: {e}", code="migration_failed")


@app.command()
def docs():
    """Regenerate AGENTS.md and the data dictionary from the spec."""
    from .core.docs import generate_agents_md, generate_data_dictionary

    proj = _project()
    spec = _spec(proj)
    proj.agents_md.write_text(generate_agents_md(spec))
    proj.build_dir.mkdir(parents=True, exist_ok=True)
    (proj.build_dir / "data_dictionary.md").write_text(generate_data_dictionary(spec))
    emit(
        {
            "agents_md": str(proj.agents_md),
            "data_dictionary": str(proj.build_dir / "data_dictionary.md"),
        }
    )


@app.command()
def schema():
    """Print the JSON Schema for the app spec (editor tooling / validation)."""
    from .core.spec import Spec

    emit(Spec.model_json_schema())


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


@app.command()
def inspect(
    entity: Optional[str] = typer.Argument(None, help="Entity to describe; omit for overview."),
):
    """Overview of the app, or full detail for one entity."""
    from .core import introspect

    proj = _project()
    spec = _spec(proj)
    if entity is None:
        emit(introspect.overview(spec), pretty=True)
    else:
        try:
            emit(introspect.describe_entity(spec, entity), pretty=True)
        except KeyError as e:
            fail(str(e), code="unknown_entity")


@app.command()
def describe(entity: str = typer.Argument(..., help="Entity (or operation) to describe.")):
    """Full schema for one entity (or inputs for one operation)."""
    from .core import introspect

    proj = _project()
    spec = _spec(proj)
    if entity in spec.entities:
        emit(introspect.describe_entity(spec, entity), pretty=True)
    elif entity in spec.operations:
        emit(introspect.describe_operation(spec, entity), pretty=True)
    else:
        fail(f"no entity or operation named {entity!r}", code="not_found")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@app.command()
def create(
    entity: str = typer.Argument(...),
    json_: str = typer.Option(..., "--json", help="Field values as a JSON object."),
):
    """Create a row of <entity>."""
    proj = _project()
    spec = _spec(proj)
    rt = _runtime(proj, spec)
    _guard_db(rt)
    _run_write(lambda: rt.create(entity, _parse_json(json_)))


@app.command()
def get(entity: str = typer.Argument(...), id: str = typer.Argument(...)):
    """Fetch one row by id."""
    proj = _project()
    spec = _spec(proj)
    rt = _runtime(proj, spec)
    _guard_db(rt)
    row = rt.get(entity, id)
    if row is None:
        fail(f"{entity} {id!r} not found", code="not_found", exit_code=1)
    emit(row)


@app.command(name="list")
def list_(
    entity: str = typer.Argument(...),
    where: list[str] = typer.Option(None, "--where", help="Filter col=value (repeatable)."),
    limit: int = typer.Option(100, "--limit"),
    offset: int = typer.Option(0, "--offset"),
    order_by: Optional[str] = typer.Option(None, "--order-by"),
    desc: bool = typer.Option(False, "--desc"),
):
    """List rows of <entity> with optional filters."""
    proj = _project()
    spec = _spec(proj)
    rt = _runtime(proj, spec)
    filters = {}
    for clause in where or []:
        if "=" not in clause:
            fail(f"--where expects col=value, got {clause!r}", code="bad_arg")
        k, v = clause.split("=", 1)
        filters[k] = v
    _guard_db(rt)
    from .core.runtime import RuntimeError_

    try:
        rows = rt.list(
            entity, filters=filters, limit=limit, offset=offset,
            order_by=order_by, descending=desc,
        )
    except RuntimeError_ as e:
        fail(str(e), code="query_error")
    emit({"entity": entity, "count": len(rows), "rows": rows})


@app.command()
def update(
    entity: str = typer.Argument(...),
    id: str = typer.Argument(...),
    json_: str = typer.Option(..., "--json", help="Fields to change as a JSON object."),
):
    """Update fields of one row."""
    proj = _project()
    spec = _spec(proj)
    rt = _runtime(proj, spec)
    _guard_db(rt)
    _run_write(lambda: rt.update(entity, id, _parse_json(json_)))


@app.command()
def delete(entity: str = typer.Argument(...), id: str = typer.Argument(...)):
    """Delete one row by id."""
    proj = _project()
    spec = _spec(proj)
    rt = _runtime(proj, spec)
    _guard_db(rt)
    _run_write(lambda: rt.delete(entity, id))


# ---------------------------------------------------------------------------
# Operations / query / import / audit
# ---------------------------------------------------------------------------


@app.command()
def do(
    operation: str = typer.Argument(...),
    json_: str = typer.Option("{}", "--json", help="Operation inputs as a JSON object."),
):
    """Run a typed custom operation."""
    proj = _project()
    spec = _spec(proj)
    rt = _runtime(proj, spec)
    _guard_db(rt)
    _run_write(lambda: rt.do(operation, _parse_json(json_)))


@app.command()
def query(
    sql: str = typer.Option(..., "--sql", help="A read-only SELECT/WITH statement."),
    param: list[str] = typer.Option(None, "--param", help="Bind param name=value (repeatable)."),
):
    """Run a read-only SQL query (or read a view); JSON rows out."""
    proj = _project()
    spec = _spec(proj)
    rt = _runtime(proj, spec)
    params = {}
    for p in param or []:
        if "=" not in p:
            fail(f"--param expects name=value, got {p!r}", code="bad_arg")
        k, v = p.split("=", 1)
        params[k] = v
    _guard_db(rt)
    from .core.runtime import RuntimeError_

    try:
        rows = rt.query(sql, params)
    except RuntimeError_ as e:
        fail(str(e), code="query_error")
    except Exception as e:
        fail(f"query failed: {e}", code="query_error")
    emit({"count": len(rows), "rows": rows})


@app.command(name="import")
def import_(
    file: str = typer.Argument(..., help="CSV / JSON / Excel file to ingest."),
    entity: str = typer.Option(..., "--entity", help="Target entity."),
    map_: list[str] = typer.Option(None, "--map", help="Override mapping source=target (repeatable)."),
    dedup: list[str] = typer.Option(None, "--dedup", help="Column(s) to dedup on (repeatable)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the mapping without inserting."),
):
    """Ingest a CSV/JSON/Excel file into <entity> with mapping + dedup."""
    from .core import ingest

    proj = _project()
    spec = _spec(proj)
    mapping = {}
    for m in map_ or []:
        if "=" not in m:
            fail(f"--map expects source=target, got {m!r}", code="bad_arg")
        s, t = m.split("=", 1)
        mapping[s] = t
    try:
        prepared = ingest.prepare(spec, entity, file, mapping=mapping)
    except (FileNotFoundError, ValueError) as e:
        fail(str(e), code="ingest_error")
    if dry_run:
        emit(
            {
                "dry_run": True,
                "mapping": prepared["mapping"],
                "unmapped_source": prepared["unmapped_source"],
                "unmapped_target": prepared["unmapped_target"],
                "row_count": prepared["row_count"],
                "sample": prepared["rows"][:3],
            },
            pretty=True,
        )
        return
    rt = _runtime(proj, spec)
    _guard_db(rt)
    result = rt.import_rows(entity, prepared["rows"], dedup_on=dedup or None)
    result["mapping"] = prepared["mapping"]
    emit(result)


@app.command()
def audit(
    entity: Optional[str] = typer.Option(None, "--entity", help="Filter by entity."),
    limit: int = typer.Option(50, "--limit"),
):
    """Show recent changes from the audit log."""
    proj = _project()
    spec = _spec(proj)
    rt = _runtime(proj, spec)
    _guard_db(rt)
    emit({"entries": rt.audit(entity=entity, limit=limit)})


# ---------------------------------------------------------------------------
# Shared write/db helpers
# ---------------------------------------------------------------------------


def _guard_db(rt) -> None:
    from .core import db

    if not db.can_connect(rt.engine):
        fail("cannot connect to the database. Run `dbmachine up` first.", code="no_db")


def _run_write(thunk) -> None:
    """Execute a write, translating core/DB errors into structured JSON."""
    from sqlalchemy.exc import SQLAlchemyError

    from .core.ops import OpNotFound
    from .core.runtime import RuntimeError_
    from .core.validation import ValidationFailed

    try:
        emit(thunk())
    except ValidationFailed as e:
        fail("input validation failed", code="validation", errors=e.errors)
    except RuntimeError_ as e:
        fail(str(e), code="runtime")
    except OpNotFound as e:
        fail(str(e), code="op_not_found")
    except SQLAlchemyError as e:
        # constraint violations (e.g. double-booking) land here — the DB is the guard
        msg = getattr(getattr(e, "orig", None), "args", [str(e)])
        fail(_db_error_message(e), code="db_constraint", detail=str(msg[0]) if msg else str(e))
    except Exception as e:  # last resort
        fail(f"{type(e).__name__}: {e}", code="unexpected")


def _db_error_message(e) -> str:
    orig = getattr(e, "orig", None)
    text = str(orig) if orig else str(e)
    first = text.strip().splitlines()[0] if text.strip() else "database error"
    return first


def main() -> None:  # console-script convenience
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
