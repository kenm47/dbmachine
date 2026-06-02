# dbmachine

**Turn a Postgres database into a self-describing, agent-operable application backend.**

Most business software is a database + a thin backend + a frontend. When the
frontend is a chat agent (like Claude Code), the missing piece is a *standard,
safe contract between Postgres and the agent*. dbmachine is that contract: a
coding agent declares an app in a YAML spec, dbmachine compiles it into a real
Postgres backend (schema, constraints, RLS, typed operations, audit log), and
generates instructions the agent reads to drive it through one self-documenting
CLI.

The guarantees live in the database, not the prompt. The agent **cannot corrupt
your data even when it's wrong** — the DB rejects illegal states.

- 🐘 **Real Postgres**, fully local (Docker), fully open source. No Supabase, no
  third-party accounts.
- 🤖 **Agent-native**: rich descriptions everywhere, granular introspection, and
  a CLI that emits machine-readable JSON for every command.
- 🔒 **Structural safety**: required fields, enums, uniqueness, check &
  **exclusion** constraints (e.g. no double-booking), row-level security, and a
  full audit log — all compiled from the spec.
- 🧱 **Clean core**: the CLI is a thin adapter over a structured-return Python
  core, so a future MCP server is a ~100-line drop-in.

> **Status:** v1. Single trusted operator, CLI-only. HTTP API, MCP server, and
> multi-tenant end-user auth are deliberately deferred (see `SPEC.md`).

> 🤖 **Building an app with an AI agent? Point it at
> [`docs/AGENT_GUIDE.md`](docs/AGENT_GUIDE.md).** That single, self-contained
> document teaches a zero-context agent how to author the spec and stand up a CRM
> or booking app end to end — the spec reference, the full CLI, custom
> operations, migrations, error codes, and copy-pasteable recipes.

## Install & bootstrap

dbmachine is a **project-local dependency managed by [`uv`](https://docs.astral.sh/uv/)**,
not a global tool. You bootstrap once with `uvx`, then everything runs in the
project's own environment — so custom operations can freely `import stripe` /
`import pandas` (`uv add ...`).

```bash
uvx dbmachine init my_crm     # scaffold a project (bootstrap only)
cd my_crm
uv sync                       # set up the project environment
dbmachine up                  # start local Postgres (auto-detects a free port)
dbmachine migrate             # compile the spec → create the schema
dbmachine inspect             # see what you've got
```

`dbmachine up` auto-detects a free host port (dodging the classic 5432 collision
with a Homebrew Postgres) and persists it to `.dbmachine/config.json`.

## "Claude, build me a CRM" — the walkthrough

The agent authors `app.dbm.yaml`, then operates the app entirely through the CLI.
Here is the exact contract, using the bundled CRM example (`examples/crm`):

```bash
# 1. Compile + migrate
dbmachine migrate
# {"ok": true, "data": {"applied": true, "fresh": true, ...}}

# 2. Learn the schema (pull only what you need)
dbmachine inspect
dbmachine describe deal

# 3. Create data — validated before it hits the DB
dbmachine create company --json '{"name": "Globex"}'
# {"ok": true, "data": {"id": "…", "name": "Globex", ...}}

dbmachine create deal --json '{"title": "Big", "amount": 5000, "stage": "proposal", "company_id": "…"}'

# 4. Run a typed custom operation (Python, transactional, with side-effect stubs)
dbmachine do win_deal --json '{"deal_id": "…"}'
# {"ok": true, "data": {"result": {"stage": "won", ...}}}

# 5. Read-only analytics via views
dbmachine query --sql 'SELECT * FROM pipeline_by_stage'

# 6. Bulk import (CSV/JSON/Excel) with heuristic mapping + dedup
dbmachine import contacts.csv --entity contact --dedup email

# 7. Full change history
dbmachine audit --entity deal
```

Every command takes `--json` input where relevant and emits a
`{"ok": true|false, ...}` envelope on stdout — reliable for an agent to parse.

## The spec (`app.dbm.yaml`)

A declarative, version-controlled source of truth. Excerpt from the bookings
example, showing the structural no-double-booking guarantee:

```yaml
entities:
  appointment:
    description: A booking of one resource for one customer over a time window.
    fields:
      starts_at: { type: timestamptz, required: true }
      ends_at:   { type: timestamptz, required: true }
      status:    { type: enum, enum: appointment_status, default: pending }
    relationships:
      customer:  { belongs_to: customer, required: true }
      resource:  { belongs_to: resource, required: true }
    checks:
      - { name: ends_after_starts, expr: "ends_at > starts_at" }
    exclusions:
      - name: no_double_booking
        with:
          - { field: resource_id, op: "=" }
          - { expr: "tstzrange(starts_at, ends_at)", op: "&&" }
        where: "status <> 'cancelled'"
```

That `exclusions` block compiles to a Postgres GiST exclusion constraint — the
agent *cannot* create overlapping bookings, no matter what it tries.

Run `dbmachine schema` to get the JSON Schema for the spec (editor tooling), and
see `examples/bookings` and `examples/crm` for complete apps.

## CLI reference

| Command | Purpose |
|---|---|
| `init [dir]` | Scaffold a new project. |
| `up` / `down` / `status` | Manage local Postgres (auto-port). |
| `compile` | Validate spec → render `schema.sql` + data dictionary. |
| `migrate [--dry-run] [--confirm]` | Diff spec vs live DB and apply (Alembic-backed; destructive changes need `--confirm`). |
| `docs` | Regenerate `AGENTS.md` + data dictionary. |
| `inspect [entity]` / `describe <entity>` | Granular introspection. |
| `create / get / list / update / delete` | Auto-CRUD per entity. |
| `do <op> --json` | Run a typed custom operation. |
| `query --sql` | Read-only SQL / views → JSON. |
| `import <file> --entity` | Ingest CSV/JSON/Excel with mapping + dedup. |
| `audit` | Show the change log. |

## How it works

```
app.dbm.yaml ──► Compiler ──► Postgres DDL (tables, FKs, checks, exclusions,
   (spec)          │           enum types, RLS, updated_at + audit triggers)
                   ├──► SQLAlchemy MetaData ──► Alembic migrations (dry-run + confirm)
                   ├──► Pydantic models ──► input validation
                   └──► AGENTS.md + data dictionary ──► the agent reads these
```

The compiler does **not** hand-roll a declarative diff engine — table/column
changes are diffed against the live DB with Alembic and kept in a review loop via
`migrate --dry-run`. dbmachine-managed objects (audit/actions tables, triggers,
views, RLS) are applied idempotently on each migrate.

## Safety model (v1)

The v1 threat model is a **trusted operator running their own agent on their own
machine**. RLS compiles `operator` vs `agent` roles for structural correctness
and forward-compatibility with a future hosted multi-tenant API — it is **not** a
security boundary against a locally-credentialed agent (which could always
escalate). The real guarantees are the DB constraints, the audit log, and
spec-as-source-of-truth in version control.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
uv pip install pgserver           # embedded Postgres for the integration suite
pytest                            # unit + integration; Postgres tests auto-skip if unavailable
```

Tests requiring the `btree_gist` extension (the exclusion-constraint invariant)
run in CI against stock `postgres:16`; the embedded test Postgres omits contrib
and those tests skip locally.

## FAQ

**If the agent can author the spec, what stops it from changing the guarantees first?**

The schema change pathway is structurally inaccessible to the agent — not just trust-based.

The spec (`app.dbm.yaml`) lives in version control, not in the database. The `migrate` command — the only thing that turns spec changes into live schema — is operator-only and not exposed to the agent role. So even if an agent modified the YAML file, nothing changes in the database until a human runs `dbmachine migrate`.

More importantly, the constraints that matter (CHECK, UNIQUE, FK, GiST exclusion constraints) live inside Postgres, not in the application layer. The agent connects via a restricted `dbm_agent` database role with SELECT/INSERT/UPDATE grants but no DDL permissions. It physically cannot issue `ALTER TABLE` or drop a constraint — the database rejects it regardless of what the agent tries.

The separation is operator vs. agent as distinct trust tiers, enforced by:

1. **`migrate` is operator-only** — the agent can edit the spec file, but only the operator can run `migrate` to apply changes to the live database
2. **Database role permissions** — the agent role has no DDL rights
3. **Postgres-level structural constraints** — no prompt engineering overrides a GiST exclusion constraint

The guarantees live in the database. The agent can't subvert them first because it never had access to change them at all.

**What's the UX for making a schema change? Does the agent submit a PR, then the operator runs migrate?**

Essentially yes, but one step simpler than you might expect.

The agent *can* edit `app.dbm.yaml` directly — it's just a file. What it can't do is run `dbmachine migrate`, which is the command that applies spec changes to the live database. That's the gate.

The simplest flow:

1. Agent edits the spec (adds the new field, entity, constraint, etc.)
2. Agent tells the operator: "I've updated the spec — please run `dbmachine migrate`"
3. Operator runs `dbmachine migrate --dry-run` to preview, then runs it for real

There's no separate "create the migration" step — Alembic automatically diffs the spec against the live database and generates the SQL. Destructive changes (dropping a column, renaming a table) require an explicit `--confirm` flag so nothing data-loss happens silently.

A PR-based workflow is the natural pattern for any production setup: agent proposes the spec change as a PR, operator reviews and merges, then runs `migrate`. But that's a process convention, not something dbmachine enforces — in a local/trusted setup the agent can just edit the file in place.

## License

Apache-2.0. See [LICENSE](LICENSE).
