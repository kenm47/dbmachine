"""The app spec: declarative source of truth for a dbmachine application.

A spec is authored in YAML (``app.dbm.yaml``), validated against these Pydantic
v2 models, and compiled into a real Postgres backend. The models below *are* the
schema; ``dbmachine schema`` emits the equivalent JSON Schema for editor tooling.

Design notes
------------
* Every object carries an optional ``description`` — rich natural-language
  metadata is what makes the app agent-native (it drives the data dictionary).
* Field/entity names are validated as SQL-safe identifiers up front so the
  compiler never has to defend against injection from the spec itself.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# Postgres reserved-ish words we refuse as identifiers to avoid quoting pain.
_RESERVED = {
    "select", "from", "where", "table", "column", "order", "group", "user",
    "default", "primary", "foreign", "constraint", "index", "view", "grant",
}


def _check_identifier(value: str, what: str) -> str:
    if not _IDENT_RE.match(value):
        raise ValueError(
            f"{what} {value!r} must be lower_snake_case, start with a letter, "
            "and contain only [a-z0-9_]"
        )
    if value in _RESERVED:
        raise ValueError(f"{what} {value!r} is a reserved word; choose another name")
    return value


class FieldType(str, Enum):
    """Logical field types mapped to Postgres column types by the compiler."""

    text = "text"
    integer = "integer"
    bigint = "bigint"
    numeric = "numeric"
    boolean = "boolean"
    timestamptz = "timestamptz"
    date = "date"
    time = "time"
    uuid = "uuid"
    jsonb = "jsonb"
    enum = "enum"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EnumDef(_Base):
    description: str | None = None
    values: list[str] = Field(min_length=1)

    @field_validator("values")
    @classmethod
    def _vals(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("enum values must be unique")
        return v


# ---------------------------------------------------------------------------
# Fields & relationships
# ---------------------------------------------------------------------------


class FieldDef(_Base):
    type: FieldType
    description: str | None = None
    required: bool = False
    unique: bool = False
    default: Any | None = None
    enum: str | None = None  # name of an EnumDef, required when type == enum

    @model_validator(mode="after")
    def _enum_consistency(self) -> "FieldDef":
        if self.type is FieldType.enum and not self.enum:
            raise ValueError("fields of type 'enum' must reference an 'enum:' name")
        if self.type is not FieldType.enum and self.enum:
            raise ValueError("only fields of type 'enum' may set 'enum:'")
        return self


RelKind = Literal["belongs_to", "has_many", "many_to_many"]


class RelationshipDef(_Base):
    belongs_to: str | None = None
    has_many: str | None = None
    many_to_many: str | None = None
    required: bool = False
    on_delete: Literal["cascade", "restrict", "set_null"] = "restrict"
    description: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "RelationshipDef":
        set_kinds = [
            k for k in ("belongs_to", "has_many", "many_to_many")
            if getattr(self, k) is not None
        ]
        if len(set_kinds) != 1:
            raise ValueError(
                "a relationship must declare exactly one of belongs_to / "
                "has_many / many_to_many"
            )
        if self.required and set_kinds[0] != "belongs_to":
            raise ValueError("only belongs_to relationships may be 'required'")
        return self

    @property
    def kind(self) -> RelKind:
        for k in ("belongs_to", "has_many", "many_to_many"):
            if getattr(self, k) is not None:
                return k  # type: ignore[return-value]
        raise AssertionError  # guarded by validator

    @property
    def target(self) -> str:
        return getattr(self, self.kind)


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


class CheckConstraint(_Base):
    name: str
    expr: str
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _n(cls, v: str) -> str:
        return _check_identifier(v, "check constraint name")


class UniqueConstraint(_Base):
    name: str
    fields: list[str] = Field(min_length=1)
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _n(cls, v: str) -> str:
        return _check_identifier(v, "unique constraint name")


class IndexDef(_Base):
    name: str
    fields: list[str] = Field(min_length=1)
    unique: bool = False

    @field_validator("name")
    @classmethod
    def _n(cls, v: str) -> str:
        return _check_identifier(v, "index name")


class ExclusionElement(_Base):
    """One element of a GiST exclusion constraint.

    Either a plain ``field`` (rendered as the column) or a raw ``expr``,
    paired with an operator (``=`` for equality, ``&&`` for range overlap).
    """

    field: str | None = None
    expr: str | None = None
    op: str

    @model_validator(mode="after")
    def _one(self) -> "ExclusionElement":
        if bool(self.field) == bool(self.expr):
            raise ValueError("exclusion element needs exactly one of field/expr")
        return self


class ExclusionConstraint(_Base):
    """A Postgres exclusion constraint — the structural no-double-booking guard."""

    name: str
    description: str | None = None
    using: str = "gist"
    with_: list[ExclusionElement] = Field(alias="with", min_length=1)
    where: str | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("name")
    @classmethod
    def _n(cls, v: str) -> str:
        return _check_identifier(v, "exclusion constraint name")


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


class EntityDef(_Base):
    description: str | None = None
    fields: dict[str, FieldDef] = Field(default_factory=dict)
    relationships: dict[str, RelationshipDef] = Field(default_factory=dict)
    checks: list[CheckConstraint] = Field(default_factory=list)
    unique: list[UniqueConstraint] = Field(default_factory=list)
    indexes: list[IndexDef] = Field(default_factory=list)
    exclusions: list[ExclusionConstraint] = Field(default_factory=list)

    @field_validator("fields", "relationships")
    @classmethod
    def _idents(cls, v: dict) -> dict:
        for name in v:
            _check_identifier(name, "field/relationship name")
        return v


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


class OperationInput(_Base):
    type: FieldType
    required: bool = False
    description: str | None = None
    enum: str | None = None
    default: Any | None = None


class OperationDef(_Base):
    description: str | None = None
    inputs: dict[str, OperationInput] = Field(default_factory=dict)
    implementation: str | None = None  # dotted path, e.g. "ops.confirm_appointment"
    entities: list[str] = Field(default_factory=list)

    @field_validator("inputs")
    @classmethod
    def _idents(cls, v: dict) -> dict:
        for name in v:
            _check_identifier(name, "operation input name")
        return v


# ---------------------------------------------------------------------------
# Views / policies / seeds
# ---------------------------------------------------------------------------


class ViewDef(_Base):
    description: str | None = None
    sql: str


class EntityPolicy(_Base):
    select: bool = True
    insert: bool = True
    update: bool = True
    delete: bool = False


class RolePolicy(_Base):
    description: str | None = None
    entities: dict[str, EntityPolicy] = Field(default_factory=dict)


class AppMeta(_Base):
    name: str
    description: str | None = None
    version: str = "0.1.0"

    @field_validator("name")
    @classmethod
    def _n(cls, v: str) -> str:
        return _check_identifier(v, "app name")


# ---------------------------------------------------------------------------
# The full spec
# ---------------------------------------------------------------------------


class Spec(_Base):
    app: AppMeta
    enums: dict[str, EnumDef] = Field(default_factory=dict)
    entities: dict[str, EntityDef] = Field(default_factory=dict)
    operations: dict[str, OperationDef] = Field(default_factory=dict)
    views: dict[str, ViewDef] = Field(default_factory=dict)
    seeds: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    policies: dict[str, RolePolicy] = Field(default_factory=dict)

    @field_validator("enums", "entities", "operations", "views")
    @classmethod
    def _idents(cls, v: dict) -> dict:
        for name in v:
            _check_identifier(name, "top-level name")
        return v

    # -- cross-reference validation ----------------------------------------

    @model_validator(mode="after")
    def _cross_refs(self) -> "Spec":
        errors: list[str] = []

        for ename, entity in self.entities.items():
            for fname, fld in entity.fields.items():
                if fld.type is FieldType.enum and fld.enum not in self.enums:
                    errors.append(
                        f"{ename}.{fname} references unknown enum {fld.enum!r}"
                    )
            for rname, rel in entity.relationships.items():
                if rel.target not in self.entities:
                    errors.append(
                        f"{ename}.{rname} references unknown entity {rel.target!r}"
                    )
            # constraint field references
            field_names = set(entity.fields) | {
                f"{r}_id" for r, rl in entity.relationships.items()
                if rl.kind == "belongs_to"
            } | {"id", "created_at", "updated_at"}
            for uc in entity.unique:
                for f in uc.fields:
                    if f not in field_names:
                        errors.append(
                            f"{ename}.unique[{uc.name}] references unknown field {f!r}"
                        )
            for idx in entity.indexes:
                for f in idx.fields:
                    if f not in field_names:
                        errors.append(
                            f"{ename}.index[{idx.name}] references unknown field {f!r}"
                        )

        for oname, op in self.operations.items():
            for inp in op.inputs.values():
                if inp.type is FieldType.enum and inp.enum not in self.enums:
                    errors.append(
                        f"operation {oname!r} input references unknown enum {inp.enum!r}"
                    )
            for e in op.entities:
                if e not in self.entities:
                    errors.append(
                        f"operation {oname!r} touches unknown entity {e!r}"
                    )

        for role, pol in self.policies.items():
            for e in pol.entities:
                if e not in self.entities:
                    errors.append(
                        f"policy {role!r} references unknown entity {e!r}"
                    )

        if errors:
            raise ValueError("spec validation failed:\n  - " + "\n  - ".join(errors))
        return self


class SpecError(Exception):
    """Raised when a spec file cannot be parsed or fails validation."""


def load_spec(path: str) -> Spec:
    """Parse and validate a YAML spec file into a :class:`Spec`."""
    import pathlib

    p = pathlib.Path(path)
    if not p.exists():
        raise SpecError(f"spec file not found: {path}")
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:  # pragma: no cover - error formatting
        raise SpecError(f"invalid YAML in {path}: {e}") from e
    return load_spec_dict(raw)


def load_spec_dict(raw: dict[str, Any]) -> Spec:
    from pydantic import ValidationError

    try:
        return Spec.model_validate(raw)
    except ValidationError as e:
        raise SpecError(_format_validation_error(e)) from e


def _format_validation_error(e: Any) -> str:
    lines = ["spec validation failed:"]
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"])
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)
