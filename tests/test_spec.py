"""Unit tests for spec parsing & validation."""

import pytest

from dbmachine.core.spec import SpecError, load_spec_dict


def _minimal(**over):
    base = {
        "app": {"name": "demo"},
        "entities": {"thing": {"fields": {"name": {"type": "text", "required": True}}}},
    }
    base.update(over)
    return base


def test_minimal_spec_loads():
    spec = load_spec_dict(_minimal())
    assert spec.app.name == "demo"
    assert "thing" in spec.entities


def test_rejects_bad_identifier():
    with pytest.raises(SpecError):
        load_spec_dict({"app": {"name": "Demo App"}})  # capitals + space


def test_enum_field_requires_enum_ref():
    with pytest.raises(SpecError):
        load_spec_dict(
            _minimal(
                entities={"thing": {"fields": {"s": {"type": "enum"}}}}
            )
        )


def test_unknown_enum_reference_fails():
    with pytest.raises(SpecError):
        load_spec_dict(
            _minimal(
                entities={"thing": {"fields": {"s": {"type": "enum", "enum": "nope"}}}}
            )
        )


def test_relationship_unknown_target_fails():
    with pytest.raises(SpecError):
        load_spec_dict(
            _minimal(
                entities={
                    "thing": {
                        "fields": {"name": {"type": "text"}},
                        "relationships": {"owner": {"belongs_to": "ghost"}},
                    }
                }
            )
        )


def test_relationship_needs_exactly_one_kind():
    with pytest.raises(SpecError):
        load_spec_dict(
            _minimal(
                entities={
                    "thing": {
                        "fields": {"name": {"type": "text"}},
                        "relationships": {"x": {"belongs_to": "thing", "has_many": "thing"}},
                    }
                }
            )
        )


def test_unique_constraint_unknown_field_fails():
    with pytest.raises(SpecError):
        load_spec_dict(
            _minimal(
                entities={
                    "thing": {
                        "fields": {"name": {"type": "text"}},
                        "unique": [{"name": "u", "fields": ["nope"]}],
                    }
                }
            )
        )


def test_extra_keys_forbidden():
    with pytest.raises(SpecError):
        load_spec_dict(_minimal(bogus_top_level=1))


def test_bookings_example_is_valid(bookings_spec):
    assert bookings_spec.app.name == "bookings"
    assert bookings_spec.entities["appointment"].exclusions[0].name == "no_double_booking"
    rel = bookings_spec.entities["appointment"].relationships["customer"]
    assert rel.kind == "belongs_to" and rel.target == "customer"
