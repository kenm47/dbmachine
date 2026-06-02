# dbmachine — Product Spec & Build Plan

## Context

Most business software is a database + a thin backend + a frontend. When the
frontend is a chat agent (Claude Code), the missing piece is a **standard,
safe contract between Postgres and the agent** so an AI can both *build* and
*operate* an application (CRM, booking, analytics, BI) without bespoke code.

**dbmachine** is an open-source Python framework that turns a Postgres database
into a self-describing, agent-operable application backend. A coding agent
declares the app in a spec file, dbmachine compiles it into a real Postgres
backend (schema, constraints, policies, typed operations) and generates
instructions the agent reads to drive it via a CLI. The "frontend" is the chat
box; the guarantees live in the database, not the prompt.

Goal: a citizen developer can say *"Claude, use dbmachine to build me a CRM,"*
and get a working, durable backend — fully local, fully open source.

## Decisions locked (from ideation)

- **Runtime model:** Hybrid — generate a *deterministic* backend AND let the
  agent operate it at runtime. Invariants enforced in the DB; the agent can't
  corrupt data even when it's wrong.
- **Foundation:** Fully open source, self-hosted, local Postgres (Docker). **No
  Supabase / no third-party accounts.**
- **Interface:** A **CLI + generated instructions file**, *not* MCP. The
  consumer is a coding agent in a terminal; the CLI is self-documenting,
  versionable, testable, and works with any agent. All commands emit
  machine-readable JSON. The CLI is a **thin adapter over a clean Python core
  that returns structured objects**, so a future MCP server is a ~100-line
  drop-in over the same core (see Deferred).
- **Stack:** Python.
- **Distribution & execution model:** dbmachine is a **project-local dependency
  managed by `uv`**, *not* a global pipx tool. `dbmachine init` creates a
  project dir with its own `uv`-managed environment and adds `dbmachine` there;
  the runtime and **custom operations execute inside that project env**, so
  `import stripe` / `import pandas` in a custom op just works (`uv add stripe`).
  `uvx dbmachine init` (or pipx) is used **only** to bootstrap the first
  command; everything after runs in-project. This avoids the isolated-venv wall
  where custom ops can't import third-party libraries, without restricting ops
  to the standard library.
- **App definition:** A **declarative YAML spec** (validated by JSON Schema).
  Easy for an agent to author, diff, and regenerate.
- **Runtime surface:** **CLI-only** for v1 (HTTP API deferred).
- **Persona/auth:** Single trusted operator + citizen-dev **zero-config**;
  opinionated defaults. Multi-tenant auth *designed-for* but deferred.
- **Scope:** General-purpose primitives from day one; example apps used only to
  validate generality, not as the product.

## Architecture

### 1. The App Spec (`app.dbm.yaml`)
Declarative source of truth, agent-authored, version-controlled:
- `entities` — tables: fields (type, required, unique, default, **description**),
  relationships (belongs_to / has_many / many_to_many), check + composite-unique
  constraints, enums, indexes.
- `operations` — named typed actions beyond CRUD: inputs, description, entities
  touched, and an optional reference to a Python implementation. Plain CRUD is
  auto-generated.
- `policies` — authorization → compiled to Postgres **Row-Level Security**. v1
  roles: `operator` (full) and `agent` (scoped, no destructive DDL). **In v1,
  RLS is for structural correctness, validation, and forward-compatibility with
  a future hosted multi-tenant API — it is NOT a security boundary against the
  agent itself.** The v1 threat model is a trusted operator running their own
  agent on their own machine; an agent with DB credentials can always escalate
  locally, and that is acceptable for v1.
- `views` / `enums` / `seeds` — for analytics/BI and fixtures.
- **Rich natural-language descriptions everywhere** — this is what makes it
  agent-native (drives the generated data dictionary).

### 2. The Compiler
- Spec → Postgres DDL + constraints + RLS + functions/triggers.
- Spec → **migrations** with **dry-run preview** and explicit confirm for
  destructive changes. **Hard rule: do NOT build a custom declarative-diff
  engine** — mapping declarative YAML to safe, data-preserving SQL (column
  renames vs. drop+add, type casts, safe constraint changes) is a multi-year
  problem (it's the entire premise of Atlas/Hasura). **v1 uses Alembic**:
  generate migration scripts and keep a human/agent in the review loop via the
  dry-run preview. Full declarative diffing (e.g. adopting Atlas, accepting its
  Go-binary orchestration cost across macOS/Windows/Linux) is reconsidered only
  if/when it becomes a headline feature.
- Spec → **Pydantic** models for operation input validation.
- Spec → agent docs. **Granular, queryable introspection is the primary
  interface** (`dbmachine describe <entity>`, `inspect --entity orders`) so the
  agent pulls only the context it needs for the task at hand. `AGENTS.md` is a
  **thin overview + index** (the app's purpose, a list of entities/operations,
  and pointers to the granular commands) — *not* a full data-dictionary dump,
  which would consume tens of thousands of tokens and blow out the context
  window as the schema grows. All regenerated on every compile.

### 3. Operations Runtime
- Auto-CRUD per entity (create/read/update/delete/list) with validation + RLS.
- Custom operations: Python functions registered against operation names;
  receive validated inputs + a transactional DB handle; may perform side-effects
  via an **integrations/actions** abstraction (email/SMS/payment — stubbed v1).
- Everything runs in transactions; all writes recorded to an **audit log**.
  **Bulk ingestion must not write one audit row per imported row** (kills
  performance, risks transaction timeouts at scale) — the import path batches
  audit writes into the transaction block or logs a single summary record and
  bypasses per-row operational hooks.
- Invariant patterns provided (e.g. Postgres exclusion constraints → no
  double-booking) so guarantees are structural, not prompt-based.

### 4. The CLI (`dbmachine`)
- `init` — scaffold app (spec + docker-compose + instructions).
- `up` / `down` — manage local Postgres (Docker), zero-config. **Must
  auto-detect port availability** (5432 collisions with Homebrew/old projects
  are the #1 local-dev failure) and dynamically map a free host port (e.g.
  5433), persisting it to local project config so every command knows how to
  reach the right instance.
- `compile` / `migrate` — compile spec; preview & apply migrations.
- `docs` — (re)generate `AGENTS.md` + data dictionary.
- `do <operation> --json '{...}'` — run a typed operation.
- `query` — structured read (or policy-enforced SQL); JSON out.
- `import <file>` — ingest CSV/JSON/Excel.
- `inspect` / `describe <entity>` — introspection for the agent.
- Global `--json` for reliable agent parsing.

### 5. Ingestion
Arbitrary CSV/JSON/Excel → map to entities (heuristic inference + explicit
mapping; optional LLM-assisted mapping later) → validate → insert with dedup.

### 6. Safety / guardrails
DB constraints enforce invariants · RLS separates operator vs. agent · migrations
preview + confirm destructive ops · full audit log · spec-as-source-of-truth in
version control.

### Tech choices
CLI: **Typer** (thin adapter over a structured-return Python core) · Validation:
**Pydantic v2** · DB: **SQLAlchemy Core / psycopg** · Migrations: **Alembic**
(no custom diff engine) · Local DB: **Docker Compose** managed by CLI · Spec:
**YAML + JSON Schema** · Env/packaging: **`uv` project-local install**
(`uvx dbmachine init` to bootstrap only). License: **Apache-2.0** (or MIT).

### Differentiator
Existing tools (Supabase, PostgREST/Hasura, Prisma/Drizzle, Directus) expose a
DB as an API but are **not agent-native**. dbmachine's edge: semantic metadata +
safe typed operations + policy, packaged so an AI builds *and* operates the app
through one self-documenting CLI.

## Build plan (phased)

- **Phase 0 — Foundations:** repo, packaging, license, Typer CLI skeleton,
  `up`/`down` local Postgres via Docker, config loading.
- **Phase 1 — Spec & compiler core:** YAML spec + JSON Schema + Pydantic models;
  compile entities/fields/relationships/constraints/enums → Postgres DDL;
  `compile`/`migrate` with dry-run.
- **Phase 2 — Operations runtime:** auto-CRUD with validation, transactions,
  audit log; `do` + `query` with JSON output.
- **Phase 3 — Custom operations & integrations:** Python operation registration;
  actions/side-effect abstraction (email/SMS stubs); invariant patterns
  (exclusion constraints).
- **Phase 4 — Policies / RLS:** operator vs. agent roles; designed for future
  multi-tenant.
- **Phase 5 — Agent instructions generator:** `docs` → `AGENTS.md` + data
  dictionary; `inspect`/`describe`.
- **Phase 6 — Ingestion:** `import` for CSV/JSON/Excel with mapping + dedup;
  optional LLM-assisted mapping.
- **Phase 7 — Examples & validation:** build **booking** (transactional,
  invariant-heavy) and **CRM/analytics** (relational + reporting) end-to-end via
  the CLI to prove generality; write the "Claude, build me a CRM" walkthrough.
- **Phase 8 — OSS hygiene:** README, CONTRIBUTING, CI, test suite.

## Verification

- **Unit:** compiler (spec→DDL), validation, operations logic.
- **Integration (real local Postgres via Docker):** compile a sample spec; run
  CRUD + custom ops; assert invariants hold (**double-booking rejected**,
  negative inventory rejected); confirm migration dry-run + destructive-confirm.
- **E2E agent walkthrough:** a scripted (or live Claude Code) run of
  `init → compile → do → query → import` against the example apps, asserting
  JSON outputs — proves the agent contract works.

## Explicitly deferred (post-v1)

**MCP server** · generated HTTP/REST API (FastAPI) · multi-tenant end-user auth ·
hosted/managed offering · web/mobile frontends · LLM-assisted ingestion as
default.

> **On MCP:** kept deferred deliberately. Claude Code's native mode is already
> "run command → parse JSON stdout," so CLI-only is not a handicap. Because the
> CLI is a thin adapter over a structured-return Python core, an MCP server
> becomes a ~100-line adapter over that *same* core whenever a non-CLI agent
> needs it — which is precisely why there's no urgency to build it now. The
> architectural requirement (decouple core logic from interface) is in v1; the
> MCP adapter itself is not.

## Open questions to settle during build
- Exact spec schema ergonomics for relationships & operations (iterate with real
  example apps in Phase 7).
- How much ingestion mapping is heuristic vs. agent-driven in v1.
- Whether `uv` is a hard prerequisite or dbmachine falls back to `pip`/venv when
  `uv` is absent (affects the citizen-dev zero-config bootstrap).
