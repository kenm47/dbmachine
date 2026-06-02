# Contributing to dbmachine

Thanks for your interest! dbmachine is Apache-2.0 licensed; by contributing you
agree your contributions are licensed under the same terms.

## Architecture (read this first)

The cardinal rule: **the CLI does no business logic.** Everything lives in
`src/dbmachine/core/` as functions/classes that return plain Python objects;
`src/dbmachine/cli.py` is a thin Typer adapter that parses args, calls the core,
and serialises JSON. This is what keeps a future MCP server a ~100-line drop-in.

Module map:

| Module | Responsibility |
|---|---|
| `core/spec.py` | Pydantic models + validation for `app.dbm.yaml`. |
| `core/compiler.py` | spec → SQLAlchemy MetaData + extra DDL (triggers/RLS/views/seeds). |
| `core/db.py` | Connect; Alembic-based migration plan/apply. |
| `core/runtime.py` | Auto-CRUD, custom ops, query, audit, bulk import. |
| `core/validation.py` | spec → Pydantic input models. |
| `core/ops.py` / `actions.py` | Custom-operation registry + side-effect stubs. |
| `core/ingest.py` | CSV/JSON/Excel → rows with mapping + dedup. |
| `core/introspect.py` / `docs.py` | Agent-facing introspection + `AGENTS.md`. |
| `core/scaffold.py` / `docker.py` / `config.py` | Project lifecycle. |
| `cli.py` | Thin JSON adapter. |

## Non-negotiables

- **No custom declarative-diff engine.** Table/column migrations go through
  Alembic. Out-of-band objects (named `dbm_*`, plus triggers/views/RLS) are
  applied idempotently and excluded from Alembic's diff.
- **All spec identifiers are validated** as SQL-safe in `spec.py`. Never
  interpolate untrusted text into SQL elsewhere.
- **Every write is auditable**, and **bulk import must not** write one audit row
  per imported row.

## Development setup

```bash
uv venv && uv pip install -e ".[dev]"
uv pip install pgserver        # optional: embedded Postgres for integration tests
pytest
```

- Unit tests need no database.
- Integration tests are marked `@pytest.mark.postgres` and auto-skip when no
  Postgres is available.
- The exclusion-constraint test needs the `btree_gist` extension; it skips on
  the embedded test Postgres and runs in CI against `postgres:16`.

## Pull requests

- Keep the CLI/core separation intact.
- Add or update tests for behavior changes (a unit test always; an integration
  test when you touch the DB path).
- Run `pytest` and make sure existing tests pass.
