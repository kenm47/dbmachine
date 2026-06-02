"""Integration tests against a real (embedded) Postgres.

The runtime / migration / audit / import machinery is exercised against the CRM
example (no Postgres contrib needed, so it runs anywhere pgserver does). The
structural invariant — double-booking rejected by a GiST exclusion constraint —
is exercised against the bookings example and gated on ``btree_gist`` (present
in stock ``postgres:16`` / CI).
"""

import copy

import pytest

from dbmachine.core import db
from dbmachine.core.runtime import Runtime, RuntimeError_
from dbmachine.core.spec import FieldDef, FieldType
from dbmachine.core.validation import ValidationFailed

pytestmark = pytest.mark.postgres


def _migrate(proj, spec):
    plan = db.apply_migration(proj, spec)
    assert plan.applied
    return plan


# --- CRM: general runtime + migrations -------------------------------------


def test_fresh_migrate_creates_schema(crm_project):
    proj, spec = crm_project
    plan = _migrate(proj, spec)
    assert plan.fresh
    rt = Runtime(project=proj, spec=spec)
    companies = rt.list("company")
    assert {c["name"] for c in companies} == {"Acme Corp"}  # seed applied


def test_crud_roundtrip_and_audit(crm_project):
    proj, spec = crm_project
    _migrate(proj, spec)
    rt = Runtime(project=proj, spec=spec)
    co = rt.list("company")[0]

    c = rt.create("contact", {"name": "Ada", "email": "ada@acme.com", "company_id": co["id"]})
    assert c["id"]
    assert rt.get("contact", c["id"])["name"] == "Ada"

    rt.update("contact", c["id"], {"title": "CTO"})
    assert rt.get("contact", c["id"])["title"] == "CTO"

    rows = rt.list("contact", filters={"email": "ada@acme.com"})
    assert len(rows) == 1

    actions = {e["action"] for e in rt.audit(entity="contact")}
    assert {"INSERT", "UPDATE"} <= actions


def test_required_field_rejected(crm_project):
    proj, spec = crm_project
    _migrate(proj, spec)
    rt = Runtime(project=proj, spec=spec)
    with pytest.raises(ValidationFailed):
        rt.create("contact", {"name": "x"})  # missing company_id


def test_check_constraint_enforced(crm_project):
    proj, spec = crm_project
    _migrate(proj, spec)
    rt = Runtime(project=proj, spec=spec)
    co = rt.list("company")[0]["id"]
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):  # amount_non_negative
        rt.create("deal", {"title": "bad", "amount": -5, "company_id": co})


def test_unique_constraint_enforced(crm_project):
    proj, spec = crm_project
    _migrate(proj, spec)
    rt = Runtime(project=proj, spec=spec)
    from sqlalchemy.exc import IntegrityError

    rt.create("company", {"name": "Dup"})
    with pytest.raises(IntegrityError):
        rt.create("company", {"name": "Dup"})


def test_custom_operation_and_view(crm_project):
    proj, spec = crm_project
    _migrate(proj, spec)
    rt = Runtime(project=proj, spec=spec)
    co = rt.list("company")[0]["id"]
    deal = rt.create("deal", {"title": "Big", "amount": 1000, "stage": "proposal", "company_id": co})

    pipeline = rt.query("SELECT * FROM pipeline_by_stage")
    assert any(r["stage"] == "proposal" for r in pipeline)

    out = rt.do("win_deal", {"deal_id": deal["id"]})
    assert out["result"]["stage"] == "won"
    assert rt.get("deal", deal["id"])["closed_at"] is not None


def test_query_is_read_only(crm_project):
    proj, spec = crm_project
    _migrate(proj, spec)
    rt = Runtime(project=proj, spec=spec)
    with pytest.raises(RuntimeError_):
        rt.query("DELETE FROM company")


def test_bulk_import_single_audit_row(crm_project):
    proj, spec = crm_project
    _migrate(proj, spec)
    rt = Runtime(project=proj, spec=spec)
    rows = [{"name": f"Co{i}", "domain": f"co{i}.com"} for i in range(20)]
    result = rt.import_rows("company", rows)
    assert result["inserted"] == 20

    entries = rt.audit(entity="company", limit=100)
    imports = [e for e in entries if e["action"] == "IMPORT"]
    # per-row INSERT audit rows for the *imported* companies (Co0..Co19)
    imported_inserts = [
        e for e in entries
        if e["action"] == "INSERT" and str(e["changes"].get("name", "")).startswith("Co")
    ]
    assert len(imports) == 1
    assert imports[0]["changes"]["inserted"] == 20
    assert len(imported_inserts) == 0  # per-row trigger bypassed during bulk


def test_import_dedup(crm_project):
    proj, spec = crm_project
    _migrate(proj, spec)
    rt = Runtime(project=proj, spec=spec)
    rows = [{"name": "Same"}, {"name": "Same"}]
    result = rt.import_rows("company", rows, dedup_on=["name"])
    assert result["inserted"] == 1
    assert result["skipped"] == 1


def test_idempotent_remigrate_no_changes(crm_project):
    proj, spec = crm_project
    _migrate(proj, spec)
    plan, _, _ = db.plan_migration(proj, spec)
    assert not plan.fresh
    assert plan.changes == []


def test_additive_then_destructive_migration(crm_project):
    proj, spec = crm_project
    _migrate(proj, spec)

    spec2 = copy.deepcopy(spec)
    spec2.entities["company"].fields["notes"] = FieldDef(type=FieldType.text)
    plan, _, _ = db.plan_migration(proj, spec2)
    assert any("notes" in c for c in plan.changes)
    assert not plan.destructive
    db.apply_migration(proj, spec2)

    # reverting (drop column) is destructive → blocked without confirm
    plan2, _, _ = db.plan_migration(proj, spec)
    assert plan2.destructive
    with pytest.raises(PermissionError):
        db.apply_migration(proj, spec, confirm_destructive=False)
    assert db.apply_migration(proj, spec, confirm_destructive=True).applied


def test_migration_adds_enum_typed_column(crm_project):
    """A non-fresh migration that introduces a new enum + enum-typed column.

    Exercises the ordering fix: the enum TYPE must be created before Alembic
    adds the column that references it.
    """
    proj, spec = crm_project
    _migrate(proj, spec)

    spec2 = copy.deepcopy(spec)
    from dbmachine.core.spec import EnumDef

    spec2.enums["priority"] = EnumDef(values=["low", "high"])
    spec2.entities["deal"].fields["priority"] = FieldDef(
        type=FieldType.enum, enum="priority"
    )
    plan, _, _ = db.plan_migration(proj, spec2)
    assert any("priority" in c for c in plan.changes)
    db.apply_migration(proj, spec2)

    rt = Runtime(project=proj, spec=spec2)
    co = rt.list("company")[0]["id"]
    deal = rt.create("deal", {"title": "P", "company_id": co, "priority": "high"})
    assert deal["priority"] == "high"


# --- bookings: the structural no-double-booking invariant ------------------


def _book(rt, resource_id, customer_id, start, end, status="pending"):
    return rt.create(
        "appointment",
        {
            "resource_id": resource_id,
            "customer_id": customer_id,
            "starts_at": start,
            "ends_at": end,
            "status": status,
        },
    )


def test_double_booking_rejected_by_db(bookings_project):
    proj, spec = bookings_project
    _migrate(proj, spec)
    rt = Runtime(project=proj, spec=spec)
    res = rt.list("resource")[0]["id"]
    cust = rt.create("customer", {"name": "Ada", "email": "ada@x.io"})["id"]

    _book(rt, res, cust, "2030-01-01T10:00:00Z", "2030-01-01T11:00:00Z")
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):  # overlapping window, same resource
        _book(rt, res, cust, "2030-01-01T10:30:00Z", "2030-01-01T11:30:00Z")

    # adjacent (non-overlapping) booking is allowed
    assert _book(rt, res, cust, "2030-01-01T11:00:00Z", "2030-01-01T12:00:00Z")["id"]

    # a cancelled booking frees the window (WHERE status <> 'cancelled')
    rt.do("cancel_appointment", {"appointment_id": rt.list("appointment")[0]["id"]})


def test_booking_confirm_operation_and_view(bookings_project):
    proj, spec = bookings_project
    _migrate(proj, spec)
    rt = Runtime(project=proj, spec=spec)
    res = rt.list("resource")[0]["id"]
    cust = rt.create("customer", {"name": "Ada", "email": "ada@x.io"})["id"]
    appt = _book(rt, res, cust, "2031-01-01T10:00:00Z", "2031-01-01T11:00:00Z")

    out = rt.do("confirm_appointment", {"appointment_id": appt["id"]})
    assert out["result"]["status"] == "confirmed"
    assert any(a["kind"] == "email" for a in out["actions"])  # stubbed side-effect
    upcoming = rt.query("SELECT * FROM upcoming_appointments")
    assert any(r["id"] == appt["id"] for r in upcoming)
