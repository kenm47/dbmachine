# dbmachine — Agent Guide

**Audience: you, an AI coding agent, with no prior context.** This single
document is everything you need to stand up a real, durable application backend
(CRM, booking system, inventory, analytics, …) on top of Postgres using
dbmachine. Read the Quickstart, then use the Reference and Recipes as needed.

---

## 1. What dbmachine is, in one minute

dbmachine turns a **declarative YAML spec** into a **real local Postgres
backend** that you operate through a **self-documenting CLI**. You write the
spec; dbmachine compiles it into tables, constraints, row-level security, an
audit log, and typed operations. You then drive the app entirely with CLI
commands that take and return **JSON**.

The most important idea: **the guarantees live in the database, not in your
prompt.** If you try to insert a row that violates a rule (a missing required
field, a duplicate, a double-booking), Postgres rejects it and the CLI returns a
clean error. You cannot corrupt the data, even if you issue a wrong command.

Your job is two things:
1. **Author `app.dbm.yaml`** — describe the entities, fields, relationships,
   constraints, and operations of the app.
2. **Operate the app** through the CLI — create/read/update data, run
   operations, query, import files.

### The workflow loop

```
init  →  write app.dbm.yaml  →  up  →  migrate  →  operate  →  (edit spec → migrate) → …
```

---

## 2. Quickstart: a working app in 6 commands

dbmachine is run as a **project-local tool via `uv`**. Bootstrap once with
`uvx`, then everything runs in the project's own environment.

```bash
# 1. Scaffold a project (creates app.dbm.yaml, ops/, docker-compose.yaml, AGENTS.md)
uvx dbmachine init my_app
cd my_app

# 2. Set up the project environment
uv sync

# 3. Edit app.dbm.yaml to describe YOUR app (see the Reference + Recipes below)
#    ... then ...

# 4. Start local Postgres (Docker; auto-detects a free port)
dbmachine up

# 5. Create the schema from your spec
dbmachine migrate

# 6. Verify and start operating
dbmachine inspect
dbmachine create <entity> --json '{...}'
```

**Prerequisites:** `uv` (https://docs.astral.sh/uv/) and **Docker** (Docker
Desktop, or Colima with the docker CLI). `dbmachine up` runs a Postgres
container. If you only have a non-Docker Postgres, you can still `compile` (no DB
needed) but `up`/`migrate`/operate need a running database.

### Every command speaks JSON

All commands print a single JSON object to stdout:

```json
{"ok": true,  "data": { ... }}
{"ok": false, "error": {"code": "validation", "message": "...", "errors": [...]}}
```

On error the process also exits non-zero. **Always parse `ok` first.** See
§7 for the full list of error codes.

---

## 3. Authoring the spec (`app.dbm.yaml`)

The spec is the single source of truth. It is validated strictly: **unknown keys
are rejected**, and all names must be valid identifiers.

### 3.1 Top-level structure

```yaml
app:          # required: app identity
enums:        # optional: enumerated value sets
entities:     # required (in practice): your tables
operations:   # optional: typed actions beyond CRUD
views:        # optional: read-only SQL views for analytics/BI
seeds:        # optional: fixture rows inserted on migrate
policies:     # optional: operator/agent RLS roles
```

### 3.2 Naming rules (read this — it bites everyone)

Every name (app, entity, field, relationship, enum, operation, constraint) must
be **`lower_snake_case`**: start with a letter, contain only `[a-z0-9_]`.

**These words are RESERVED and cannot be used as names:**
`column`, `constraint`, `default`, `foreign`, `from`, `grant`, `group`,
`index`, `order`, `primary`, `select`, `table`, `user`, `view`, `where`.

This matters constantly for CRMs and booking apps:
- ❌ `user` → ✅ `account`, `person`, `member`
- ❌ `order` → ✅ `purchase`, `sales_order`, `booking`
- ❌ `group` → ✅ `team`, `segment`

### 3.3 `app`

```yaml
app:
  name: bookings           # required, identifier
  description: >           # optional but STRONGLY recommended (agent-readable)
    A short, rich description of what this app is for.
  version: 0.1.0           # optional, default "0.1.0"
```

Write a real `description` everywhere you can. Descriptions flow into the
generated data dictionary and `describe`/`inspect` output — they are what makes
the app understandable to an agent later.

### 3.4 `entities`

Each entity becomes a Postgres table. dbmachine **auto-adds** three columns to
every entity — do **not** declare them yourself:

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` | Primary key, auto-generated (`gen_random_uuid()`). |
| `created_at` | `timestamptz` | Set on insert. |
| `updated_at` | `timestamptz` | Auto-updated on every UPDATE (trigger). |

```yaml
entities:
  customer:
    description: A person who books appointments.
    fields:
      name:
        type: text
        required: true
        description: Full name.
      email:
        type: text
        required: true
        unique: true
        description: Contact email; unique across customers.
      phone:
        type: text            # optional (required defaults to false)
```

#### Field types

| `type` | Postgres | Pass values in JSON as |
|---|---|---|
| `text` | `text` | string |
| `integer` | `integer` | number |
| `bigint` | `bigint` | number |
| `numeric` | `numeric` | number (e.g. `1999.99`) |
| `boolean` | `boolean` | `true` / `false` |
| `timestamptz` | `timestamptz` | ISO-8601 string with tz, e.g. `"2030-01-01T10:00:00Z"` |
| `date` | `date` | `"2030-01-01"` |
| `time` | `time` | `"13:30:00"` |
| `uuid` | `uuid` | string UUID |
| `jsonb` | `jsonb` | any JSON value |
| `enum` | a custom enum type | one of the enum's values (string) |

#### Field options

```yaml
fields:
  status:
    type: enum
    enum: appointment_status   # REQUIRED when type is enum; names an entry under top-level `enums`
    default: pending           # optional; must be one of the enum values
    required: true             # optional, default false → column is NOT NULL
    unique: true               # optional, default false → adds a UNIQUE constraint
    description: ...
```

### 3.5 `enums`

```yaml
enums:
  appointment_status:
    description: Lifecycle state of an appointment.
    values: [pending, confirmed, cancelled]   # 1+ unique strings
```

⚠️ **Migration limitation:** adding a **new value** to an existing enum is not
auto-migrated (the type is created once). Choose your enum values up front. If
you must add one later, do it with a manual `ALTER TYPE ... ADD VALUE` via
`dbmachine query` is not possible (query is read-only) — connect with `psql` or
recreate. Plan ahead to avoid this.

### 3.6 `relationships`

Relationships live **inside an entity**. Declare **exactly one** kind per
relationship.

```yaml
entities:
  appointment:
    fields: { ... }
    relationships:
      customer:
        belongs_to: customer    # creates a `customer_id` uuid FK column on `appointment`
        required: true          # only belongs_to may be required → FK column is NOT NULL
        on_delete: cascade      # cascade | restrict (default) | set_null
        description: The customer who made the booking.
      resource:
        belongs_to: resource
        required: true
        on_delete: restrict
```

| Kind | What it does | Column added? |
|---|---|---|
| `belongs_to: X` | This entity holds a foreign key to X. | Yes: `<relname>_id` (uuid). |
| `has_many: X` | Inverse side (X belongs_to this). Documentation only. | No. |
| `many_to_many: X` | Link table `<a>_<b>_link` (names sorted) with both FKs. | A join table. |

**To set a relationship when creating a row, pass `<relname>_id`:**
```bash
dbmachine create appointment --json '{"customer_id":"<uuid>","resource_id":"<uuid>","starts_at":"2030-01-01T10:00:00Z","ends_at":"2030-01-01T11:00:00Z"}'
```

### 3.7 Constraints — how you make illegal states impossible

These are the heart of dbmachine. Put them under an entity.

#### `checks` — row-level boolean rules
```yaml
    checks:
      - name: ends_after_starts
        expr: "ends_at > starts_at"      # raw SQL boolean over the row's columns
        description: A booking must end after it starts.
```

#### `unique` — composite uniqueness
```yaml
    unique:
      - name: one_email_per_company
        fields: [company_id, email]
```

#### `indexes` — performance
```yaml
    indexes:
      - name: idx_appointment_starts_at
        fields: [starts_at]
        unique: false           # optional
```

#### `exclusions` — the no-double-booking guarantee
A Postgres GiST **exclusion constraint**: no two rows may both match the
combination of operators. The classic use is "a resource can't be booked for
two overlapping time windows."

```yaml
    exclusions:
      - name: no_double_booking
        using: gist                       # default; leave as-is
        with:
          - { field: resource_id, op: "=" }                  # same resource
          - { expr: "tstzrange(starts_at, ends_at)", op: "&&" }  # overlapping range
        where: "status <> 'cancelled'"    # optional: only enforce for live rows
```
Each `with` element is `{field: <col>, op: <operator>}` **or**
`{expr: <sql>, op: <operator>}`. Equality (`=`) elements require the `btree_gist`
extension, which dbmachine enables automatically and which stock `postgres:16`
includes.

### 3.8 `operations` — typed actions beyond CRUD

Use an operation when the action is more than a single-row create/update — e.g.
"confirm a booking and email the customer," "mark a deal won and stamp the close
time," "transfer stock between warehouses atomically."

```yaml
operations:
  confirm_appointment:
    description: Confirm a pending appointment.
    inputs:
      appointment_id:
        type: uuid
        required: true
        description: ID of the appointment to confirm.
    implementation: ops.confirm_appointment   # dotted path into the project's ops/ package
    entities: [appointment]                    # which entities it touches (documentation)
```

Inputs are validated (same type system as fields) **before** your Python runs.
See §5 for how to write the implementation.

### 3.9 `views` — read-only analytics/BI

```yaml
views:
  pipeline_by_stage:
    description: Open-deal count and total value per stage.
    sql: >
      SELECT stage, count(*) AS deals, COALESCE(sum(amount),0) AS total_value
      FROM deal WHERE stage NOT IN ('won','lost')
      GROUP BY stage ORDER BY total_value DESC
```
Read a view with `dbmachine query --sql 'SELECT * FROM pipeline_by_stage'`.

### 3.10 `seeds` — fixture data

Rows inserted on every `migrate` (idempotent via `ON CONFLICT DO NOTHING`, so
give seeded rows a `unique` field to avoid duplicates).

```yaml
seeds:
  resource:
    - { name: "Room A", capacity: 8 }
    - { name: "Room B", capacity: 4 }
```

### 3.11 `policies` — roles (v1: structural, not a security boundary)

Compiles to Postgres Row-Level Security for two roles: `operator` (full) and
`agent` (scoped). In v1 this is for structural correctness and forward-
compatibility, **not** a hard boundary against a locally-credentialed agent.

```yaml
policies:
  agent:
    description: The AI agent role.
    entities:
      appointment: { select: true, insert: true, update: true, delete: false }
      customer:    { select: true, insert: true, update: true, delete: false }
```
Defaults if omitted: `select/insert/update: true`, `delete: false`.

---

## 4. The CLI — complete reference

Run inside the project directory (the one containing `app.dbm.yaml`).

### Lifecycle
| Command | What it does |
|---|---|
| `dbmachine init [DIR] [--name NAME]` | Scaffold a new project (default DIR `.`). |
| `dbmachine up` | Start local Postgres (Docker), auto-detecting a free host port, persisted to `.dbmachine/config.json`. |
| `dbmachine down [--destroy]` | Stop Postgres. `--destroy` also deletes the data volume. |
| `dbmachine status` | Report whether Postgres is running/healthy. |

### Schema
| Command | What it does |
|---|---|
| `dbmachine compile [--out PATH]` | Validate the spec and render `schema.sql` + data dictionary (no DB needed). Great for checking a spec before `up`. |
| `dbmachine migrate [--dry-run] [--confirm]` | Diff the spec against the live DB and apply. `--dry-run` previews. **Destructive changes (drop table/column) require `--confirm`.** |
| `dbmachine docs` | Regenerate `AGENTS.md` + data dictionary from the spec. |
| `dbmachine schema` | Print the JSON Schema of the spec format itself. |

### Introspection (pull only what you need)
| Command | What it does |
|---|---|
| `dbmachine inspect` | Overview: app, entities, operations, views, enums. |
| `dbmachine inspect <entity>` | Full detail for one entity. |
| `dbmachine describe <entity\|operation>` | Full schema for an entity, or inputs for an operation (with an example command). |

### Data — CRUD
| Command | What it does |
|---|---|
| `dbmachine create <entity> --json '{...}'` | Insert a row. Returns the created row (with `id`). |
| `dbmachine get <entity> <id>` | Fetch one row by id. |
| `dbmachine list <entity> [--where col=val]... [--limit N] [--offset N] [--order-by col] [--desc]` | List rows. `--where` is **equality only** and repeatable; for richer filters use `query`. |
| `dbmachine update <entity> <id> --json '{...}'` | Update the given fields (at least one). |
| `dbmachine delete <entity> <id>` | Delete one row by id. |

### Operations / query / import / audit
| Command | What it does |
|---|---|
| `dbmachine do <operation> [--json '{...}']` | Run a typed custom operation (default input `{}`). |
| `dbmachine query --sql 'SELECT ...' [--param name=value]...` | **Read-only** SQL (only `SELECT`/`WITH`/`TABLE`). Use for joins, aggregates, views. Bind params with `--param`. |
| `dbmachine import <file> --entity <e> [--map src=target]... [--dedup col]... [--dry-run]` | Ingest CSV/JSON/Excel. See §6. |
| `dbmachine audit [--entity <e>] [--limit N]` | Recent changes from the audit log. |

---

## 5. Writing custom operations (Python)

When the spec references `implementation: ops.confirm_appointment`, you must
define that function in the project's `ops/` package (`ops/__init__.py` or a
submodule). Because dbmachine runs **inside the project's `uv` environment**, you
can `uv add <package>` and freely `import` third-party libraries here.

### The contract

```python
# ops/__init__.py
import sqlalchemy as sa

def confirm_appointment(ctx, *, appointment_id):
    """
    - First positional arg `ctx` is the operation context.
    - All declared `inputs` arrive as keyword arguments, already validated.
    - Runs inside a transaction: if you raise, everything rolls back.
    - Return any JSON-serialisable value (dict/list/str/number/bool/None).
    """
    row = ctx.conn.execute(
        sa.text("UPDATE appointment SET status='confirmed' WHERE id=:id RETURNING id, status"),
        {"id": appointment_id},
    ).first()
    if row is None:
        raise ValueError(f"appointment {appointment_id} not found")  # surfaces as a clean error

    # Side effects go through ctx.actions (stubbed in v1 → recorded, not really sent)
    email = ctx.conn.execute(
        sa.text("SELECT c.email FROM appointment a JOIN customer c ON c.id=a.customer_id WHERE a.id=:id"),
        {"id": appointment_id},
    ).scalar()
    if email:
        ctx.actions.email(email, "Booking confirmed", "Your appointment is confirmed.")

    return {"id": str(row.id), "status": row.status}
```

### `ctx` surface
- `ctx.conn` — a **transactional** SQLAlchemy connection. Use `sa.text("...")`
  with bound params (`:name`) — never string-format user input into SQL.
- `ctx.actions` — side-effect stubs (recorded to `dbm_actions_log`, returned in
  the operation result under `"actions"`):
  - `ctx.actions.email(to, subject, body)`
  - `ctx.actions.sms(to, message)`
  - `ctx.actions.charge(customer, amount_cents, currency="usd")`
- `ctx.operation` — the operation name (string).

Run it: `dbmachine do confirm_appointment --json '{"appointment_id":"<uuid>"}'`.

---

## 6. Importing data (CSV / JSON / Excel)

```bash
# Preview the column→field mapping without inserting:
dbmachine import customers.csv --entity customer --dry-run

# Apply, overriding/locking specific mappings and deduplicating:
dbmachine import customers.csv --entity customer --map "E-Mail=email" --dedup email
```

- Supported: `.csv`, `.json` (array of objects, or `{"rows":[...]}`), `.xlsx`.
- **Mapping** is inferred by normalized name match (case- and punctuation-
  insensitive: `"Full Name"` → `full_name` → matches field `full_name`). Use
  `--map source=target` to override or add mappings the heuristic missed.
- **`--dedup col`** (repeatable) → `ON CONFLICT DO NOTHING` on those columns;
  requires a `unique` constraint on them. Rows that collide are skipped.
- Bulk import is efficient: it bypasses the per-row audit trigger and writes a
  single `IMPORT` summary row to the audit log.
- The result reports `received`, `inserted`, `skipped`, and per-row `errors`.

---

## 7. Reading results & handling errors

Success: `{"ok": true, "data": <result>}`. Failure: `{"ok": false, "error":
{"code": <code>, "message": <human msg>, ...}}` and a non-zero exit code.

| `code` | Meaning / what to do |
|---|---|
| `no_project` | Not inside a dbmachine project. `cd` into the project or `init`. |
| `invalid_spec` | `app.dbm.yaml` failed validation. The message lists each problem (path + reason). Fix the YAML. |
| `bad_json` | The `--json` argument isn't a valid JSON object. |
| `no_db` | Can't reach Postgres. Run `dbmachine up`. |
| `docker` | Docker problem (not installed / daemon down). |
| `validation` | Input failed type/required checks. `error.errors` is `[{field, error}]`. |
| `db_constraint` | The database rejected the write (unique, check, FK, **exclusion/double-booking**). `error.detail` has the Postgres detail. This is the safety net working as designed. |
| `not_found` | Row/entity/operation doesn't exist. |
| `destructive_unconfirmed` | A migration would drop a table/column. Re-run `migrate --confirm` **only if you intend the data loss**. |
| `migration_failed` | Migration error; read the message. |
| `query_error` | Bad SQL, or you tried to write via `query` (read-only). |
| `op_not_found` | The operation's `implementation` couldn't be imported. Check `ops/`. |
| `ingest_error` | Import file missing/unsupported/malformed. |

---

## 8. Evolving the schema (migrations)

dbmachine uses **Alembic** under the hood to diff your spec against the live DB —
it does not invent a custom diff engine.

1. Edit `app.dbm.yaml`.
2. **Preview:** `dbmachine migrate --dry-run` → shows `changes` and any
   `destructive` items.
3. **Apply:** `dbmachine migrate` (additive changes) or `dbmachine migrate
   --confirm` (if destructive).

Rules of thumb:
- **Adding** entities, fields, indexes, constraints → additive, safe.
- **Removing** an entity or field → **destructive**, needs `--confirm` (data is
  lost).
- **Renaming** a field is seen as drop-old + add-new = **data loss**. To preserve
  data, add the new field, migrate, backfill with `do`/SQL, then drop the old one
  with `--confirm`.
- **Enum values** can't be added by migration (see §3.5). Decide them up front.

dbmachine-managed objects (audit/actions tables `dbm_*`, the `updated_at` and
audit triggers, views, RLS) are re-applied idempotently on every migrate.

---

## 9. Generated files (don't edit by hand)

After `init` / `migrate` / `docs` you'll see:

| Path | What it is |
|---|---|
| `app.dbm.yaml` | **Your spec — the only file you author by hand.** |
| `ops/` | Your custom-operation Python (you author this when you declare operations). |
| `AGENTS.md` | Thin, regenerated overview + index for the operating agent. |
| `docker-compose.yaml` | Generated Postgres service. |
| `.dbmachine/config.json` | Connection config (db name, the auto-chosen port, container). |
| `.dbmachine/build/schema.sql` | Full rendered DDL (for inspection). |
| `.dbmachine/build/data_dictionary.md` | Full per-column data dictionary. |

---

## 10. Recipe: stand up a **booking app**

Goal: customers book shared resources for time windows; **never double-book**.

**Step 1 — scaffold & open the spec**
```bash
uvx dbmachine init bookings && cd bookings && uv sync
```

**Step 2 — write `app.dbm.yaml`** (this is the complete file):
```yaml
app:
  name: bookings
  description: Customers book shared resources for time windows; no double-booking.

enums:
  appointment_status:
    description: Lifecycle state of an appointment.
    values: [pending, confirmed, cancelled]

entities:
  customer:
    description: A person who books appointments.
    fields:
      name:  { type: text, required: true, description: Full name. }
      email: { type: text, required: true, unique: true, description: Contact email. }
      phone: { type: text, description: Optional phone number. }

  resource:
    description: A bookable resource (room, table, staff member).
    fields:
      name:     { type: text, required: true }
      capacity: { type: integer, default: 1 }

  appointment:
    description: A booking of one resource for one customer over a time window.
    fields:
      starts_at: { type: timestamptz, required: true, description: Inclusive start. }
      ends_at:   { type: timestamptz, required: true, description: Exclusive end. }
      status:    { type: enum, enum: appointment_status, default: pending }
      notes:     { type: text }
    relationships:
      customer: { belongs_to: customer, required: true, on_delete: cascade }
      resource: { belongs_to: resource, required: true, on_delete: restrict }
    checks:
      - { name: ends_after_starts, expr: "ends_at > starts_at" }
    exclusions:
      - name: no_double_booking
        with:
          - { field: resource_id, op: "=" }
          - { expr: "tstzrange(starts_at, ends_at)", op: "&&" }
        where: "status <> 'cancelled'"

operations:
  confirm_appointment:
    description: Confirm a pending appointment and email the customer.
    inputs:
      appointment_id: { type: uuid, required: true }
    implementation: ops.confirm_appointment
    entities: [appointment]

views:
  upcoming_appointments:
    description: Confirmed future appointments, soonest first.
    sql: >
      SELECT a.id, a.starts_at, c.name AS customer, r.name AS resource
      FROM appointment a JOIN customer c ON c.id=a.customer_id
      JOIN resource r ON r.id=a.resource_id
      WHERE a.status='confirmed' AND a.starts_at > now() ORDER BY a.starts_at

seeds:
  resource:
    - { name: "Room A", capacity: 8 }
    - { name: "Room B", capacity: 4 }
```

**Step 3 — write the operation** in `ops/__init__.py` (see §5 for the body of
`confirm_appointment`).

**Step 4 — bring it up and use it**
```bash
dbmachine up
dbmachine migrate
RES=$(dbmachine list resource | <pick rows[0].id>)
CUST=$(dbmachine create customer --json '{"name":"Ada","email":"ada@x.io"}' | <pick id>)
dbmachine create appointment --json "{\"resource_id\":\"$RES\",\"customer_id\":\"$CUST\",\"starts_at\":\"2030-01-01T10:00:00Z\",\"ends_at\":\"2030-01-01T11:00:00Z\"}"
# An overlapping booking on the same resource is REJECTED by the database:
dbmachine create appointment --json "{\"resource_id\":\"$RES\",\"customer_id\":\"$CUST\",\"starts_at\":\"2030-01-01T10:30:00Z\",\"ends_at\":\"2030-01-01T11:30:00Z\"}"
# → {"ok": false, "error": {"code": "db_constraint", "message": "...no_double_booking..."}}
dbmachine do confirm_appointment --json "{\"appointment_id\":\"<id>\"}"
dbmachine query --sql 'SELECT * FROM upcoming_appointments'
```

A full, runnable version lives in `examples/bookings/` in the repo.

---

## 11. Recipe: stand up a **CRM**

Goal: companies, the people at them, and the deals in the pipeline — with
reporting.

```yaml
app:
  name: crm
  description: Companies, contacts and deals with pipeline reporting.

enums:
  deal_stage:
    description: Sales pipeline stage.
    values: [lead, qualified, proposal, won, lost]

entities:
  company:
    description: An organisation we sell to.
    fields:
      name:     { type: text, required: true, unique: true }
      domain:   { type: text }
      industry: { type: text }

  contact:                          # NOTE: 'user' is reserved — use 'contact'/'person'
    description: A person at a company.
    fields:
      name:  { type: text, required: true }
      email: { type: text, unique: true }
      title: { type: text }
    relationships:
      company: { belongs_to: company, required: true, on_delete: cascade }

  deal:                             # NOTE: 'order' is reserved — use 'deal'/'purchase'
    description: A potential or closed sale.
    fields:
      title:     { type: text, required: true }
      amount:    { type: numeric, default: 0 }
      stage:     { type: enum, enum: deal_stage, default: lead }
      closed_at: { type: timestamptz }
    relationships:
      company: { belongs_to: company, required: true, on_delete: cascade }
    checks:
      - { name: amount_non_negative, expr: "amount >= 0" }
    indexes:
      - { name: idx_deal_stage, fields: [stage] }

operations:
  win_deal:
    description: Mark a deal as won and stamp the close time.
    inputs:
      deal_id: { type: uuid, required: true }
    implementation: ops.win_deal
    entities: [deal]

views:
  pipeline_by_stage:
    description: Open-deal count and total value per stage.
    sql: >
      SELECT stage, count(*) AS deals, COALESCE(sum(amount),0) AS total_value
      FROM deal WHERE stage NOT IN ('won','lost')
      GROUP BY stage ORDER BY total_value DESC
```

`ops/__init__.py`:
```python
import sqlalchemy as sa

def win_deal(ctx, *, deal_id):
    row = ctx.conn.execute(
        sa.text("UPDATE deal SET stage='won', closed_at=now() WHERE id=:id RETURNING id, amount, stage"),
        {"id": deal_id},
    ).first()
    if row is None:
        raise ValueError(f"deal {deal_id} not found")
    return {"id": str(row.id), "amount": float(row.amount), "stage": row.stage}
```

Operate it:
```bash
dbmachine up && dbmachine migrate
dbmachine create company --json '{"name":"Globex","domain":"globex.com"}'
dbmachine create deal --json '{"title":"Renewal","amount":12000,"stage":"proposal","company_id":"<company id>"}'
dbmachine query --sql 'SELECT * FROM pipeline_by_stage'
dbmachine do win_deal --json '{"deal_id":"<deal id>"}'
# Import a contact list:
dbmachine import contacts.csv --entity contact --dedup email
```

A full, runnable version lives in `examples/crm/` in the repo.

---

## 12. A reliable end-to-end procedure (follow this)

When asked to build an app, do this in order:

1. **Clarify the domain** briefly: the core entities, how they relate, the rules
   that must always hold (uniqueness, no-overlap, non-negative, required links).
2. `uvx dbmachine init <name> && cd <name> && uv sync`.
3. **Write `app.dbm.yaml`**: entities + fields (with descriptions), relationships,
   then encode every rule as a `check` / `unique` / `exclusion`. Avoid reserved
   names (§3.2). Pick enum values you won't need to change.
4. If any action is multi-step or has side effects, add an `operation` and write
   its function in `ops/`.
5. **Validate offline:** `dbmachine compile`. Fix any `invalid_spec` errors.
6. `dbmachine up` then `dbmachine migrate`. (Use `migrate --dry-run` first when
   changing an existing app.)
7. **Verify the guarantees**, don't assume them: create a valid row (expect
   `ok:true`), then deliberately violate a rule (duplicate, overlap, missing
   required) and confirm you get `ok:false` with `db_constraint`/`validation`.
8. Seed/import any starting data; expose reporting as `views`.
9. Run `dbmachine docs` so `AGENTS.md` reflects the final app.
10. Report what you built, the invariants enforced, and the exact commands to
    operate it.

**Golden rule:** push every business rule into the spec as a constraint. If a
rule is only "remembered" in prose, it isn't enforced. If it's a `check`,
`unique`, or `exclusion`, the database guarantees it forever.
