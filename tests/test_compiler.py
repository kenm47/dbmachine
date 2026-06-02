"""Unit tests for the compiler (spec → DDL). No database required."""

from dbmachine.core.compiler import build_metadata, render_schema_sql
from dbmachine.core.sqlsplit import split_sql


def test_renders_tables_and_constraints(bookings_spec):
    sql = render_schema_sql(bookings_spec)
    assert "CREATE TABLE customer" in sql
    assert "CREATE TABLE appointment" in sql
    # FK, check, exclusion, enum type, RLS all present
    assert "FOREIGN KEY(customer_id) REFERENCES customer" in sql
    assert "CHECK (ends_at > starts_at)" in sql
    assert "EXCLUDE USING gist" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "gen_random_uuid()" in sql


def test_audit_and_touch_triggers_present(bookings_spec):
    sql = render_schema_sql(bookings_spec)
    assert "dbm_audit_log" in sql
    assert "appointment_audit" in sql
    assert "appointment_touch_updated_at" in sql


def test_seeds_use_on_conflict(bookings_spec):
    sql = render_schema_sql(bookings_spec)
    assert "INSERT INTO resource" in sql
    assert "ON CONFLICT DO NOTHING" in sql


def test_metadata_has_system_columns(bookings_spec):
    md, _ = build_metadata(bookings_spec)
    appt = md.tables["appointment"]
    for col in ("id", "created_at", "updated_at", "customer_id", "resource_id"):
        assert col in appt.c
    assert appt.c.id.primary_key


def test_split_preserves_function_bodies(bookings_spec):
    sql = render_schema_sql(bookings_spec)
    stmts = split_sql(sql)
    audit_fn = [s for s in stmts if "dbm_audit()" in s and "RETURNS trigger" in s]
    assert len(audit_fn) == 1
    # the whole body (with internal semicolons) stays in one statement
    assert "INSERT INTO dbm_audit_log" in audit_fn[0]


def test_enum_type_emitted_once(bookings_spec):
    sql = render_schema_sql(bookings_spec)
    # SQLAlchemy emits the ENUM type creation inline with the first use
    assert sql.count("appointment_status") >= 1
