"""Unit tests for validation, ingestion, introspection and docs."""

import pytest

from dbmachine.core import docs, ingest, introspect
from dbmachine.core.validation import (
    ValidationFailed,
    entity_input_model,
    validate,
)


def test_create_model_requires_required_fields(bookings_spec):
    model = entity_input_model(bookings_spec, "customer", partial=False)
    with pytest.raises(ValidationFailed) as ei:
        validate(model, {"phone": "555"})  # missing name, email
    fields = {e["field"] for e in ei.value.errors}
    assert {"name", "email"} <= fields


def test_update_model_is_partial(bookings_spec):
    model = entity_input_model(bookings_spec, "customer", partial=True)
    out = validate(model, {"phone": "555"})
    assert out == {"phone": "555"}  # only provided keys


def test_enum_value_validated(bookings_spec):
    model = entity_input_model(bookings_spec, "appointment", partial=True)
    with pytest.raises(ValidationFailed):
        validate(model, {"status": "bogus"})


def test_extra_field_rejected(bookings_spec):
    model = entity_input_model(bookings_spec, "customer", partial=False)
    with pytest.raises(ValidationFailed):
        validate(model, {"name": "A", "email": "a@b.c", "nope": 1})


def test_ingest_infers_mapping(bookings_spec, tmp_path):
    csv = tmp_path / "c.csv"
    csv.write_text("Name,E-Mail,Phone\nAda,ada@x.io,555\n")
    prepared = ingest.prepare(bookings_spec, "customer", str(csv))
    assert prepared["mapping"] == {"Name": "name", "E-Mail": "email", "Phone": "phone"}
    assert prepared["rows"][0]["email"] == "ada@x.io"


def test_ingest_explicit_mapping_overrides(bookings_spec, tmp_path):
    csv = tmp_path / "c.csv"
    csv.write_text("full,contact\nAda,ada@x.io\n")
    prepared = ingest.prepare(
        bookings_spec, "customer", str(csv), mapping={"full": "name", "contact": "email"}
    )
    assert prepared["rows"][0] == {"name": "Ada", "email": "ada@x.io"}


def test_ingest_json(bookings_spec, tmp_path):
    j = tmp_path / "c.json"
    j.write_text('[{"name": "Ada", "email": "ada@x.io"}]')
    prepared = ingest.prepare(bookings_spec, "customer", str(j))
    assert prepared["row_count"] == 1


def test_describe_entity_includes_system_fields(bookings_spec):
    d = introspect.describe_entity(bookings_spec, "appointment")
    names = {f["name"] for f in d["fields"]}
    assert {"id", "created_at", "updated_at", "starts_at"} <= names
    assert d["exclusions"][0]["name"] == "no_double_booking"


def test_agents_md_is_thin_index(bookings_spec):
    md = docs.generate_agents_md(bookings_spec)
    # index + pointers, not a full column dump
    assert "dbmachine describe customer" in md
    assert "## Entities" in md
    # should NOT enumerate every column (that's the data dictionary's job)
    assert "| field | type |" not in md


def test_data_dictionary_has_columns(bookings_spec):
    dd = docs.generate_data_dictionary(bookings_spec)
    assert "| field | type |" in dd
    assert "`starts_at`" in dd
