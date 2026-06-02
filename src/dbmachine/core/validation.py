"""Spec → Pydantic models for input validation.

Builds dynamic Pydantic v2 models for entity create/update payloads and for
custom-operation inputs, so the runtime rejects malformed input *before* it
reaches Postgres with a clear, structured error.
"""

from __future__ import annotations

import datetime as _dt
import uuid as _uuid
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, create_model

from .spec import EntityDef, FieldType, OperationDef, Spec

_PY_TYPE = {
    FieldType.text: str,
    FieldType.integer: int,
    FieldType.bigint: int,
    FieldType.numeric: Decimal,
    FieldType.boolean: bool,
    FieldType.timestamptz: _dt.datetime,
    FieldType.date: _dt.date,
    FieldType.time: _dt.time,
    FieldType.uuid: _uuid.UUID,
    FieldType.jsonb: Any,
}


class ValidationFailed(Exception):
    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__("input validation failed")


def _enum_type(spec: Spec, enum_name: str):
    values = tuple(spec.enums[enum_name].values)
    return Literal[values]  # type: ignore[valid-type]


def entity_input_model(spec: Spec, ename: str, *, partial: bool) -> type[BaseModel]:
    """Build a create (partial=False) or update (partial=True) model for an entity."""
    entity: EntityDef = spec.entities[ename]
    fields: dict[str, tuple] = {}

    for fname, fld in entity.fields.items():
        if fld.type is FieldType.enum:
            pytype = _enum_type(spec, fld.enum)
        else:
            pytype = _PY_TYPE[fld.type]
        required = fld.required and fld.default is None and not partial
        if required:
            fields[fname] = (pytype, ...)
        else:
            fields[fname] = (pytype | None, None)

    for rname, rel in entity.relationships.items():
        if rel.kind != "belongs_to":
            continue
        col = f"{rname}_id"
        required = rel.required and not partial
        if required:
            fields[col] = (_uuid.UUID, ...)
        else:
            fields[col] = (_uuid.UUID | None, None)

    model = create_model(  # type: ignore[call-overload]
        f"{ename.title()}{'Update' if partial else 'Create'}",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
    return model


def operation_input_model(spec: Spec, oname: str) -> type[BaseModel]:
    op: OperationDef = spec.operations[oname]
    fields: dict[str, tuple] = {}
    for iname, inp in op.inputs.items():
        if inp.type is FieldType.enum:
            pytype = _enum_type(spec, inp.enum)
        else:
            pytype = _PY_TYPE[inp.type]
        if inp.required and inp.default is None:
            fields[iname] = (pytype, ...)
        else:
            fields[iname] = (pytype | None, inp.default)
    return create_model(  # type: ignore[call-overload]
        f"Op_{oname}_Input",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


def validate(model: type[BaseModel], data: dict) -> dict:
    from pydantic import ValidationError

    try:
        obj = model.model_validate(data)
    except ValidationError as e:
        raise ValidationFailed(
            [
                {"field": ".".join(str(x) for x in err["loc"]), "error": err["msg"]}
                for err in e.errors()
            ]
        ) from e
    # exclude_unset so updates only touch provided columns
    return obj.model_dump(exclude_unset=True, mode="json")
