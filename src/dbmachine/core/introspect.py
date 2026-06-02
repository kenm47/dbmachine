"""Granular, queryable introspection — the primary agent interface.

The agent pulls only the context it needs (`describe <entity>`, `inspect`)
rather than ingesting a giant data-dictionary dump that would blow out the
context window as the schema grows.
"""

from __future__ import annotations

from typing import Any

from .spec import Spec


def overview(spec: Spec) -> dict[str, Any]:
    return {
        "app": {
            "name": spec.app.name,
            "description": spec.app.description,
            "version": spec.app.version,
        },
        "entities": [
            {"name": n, "description": e.description, "fields": len(e.fields)}
            for n, e in spec.entities.items()
        ],
        "operations": [
            {"name": n, "description": o.description} for n, o in spec.operations.items()
        ],
        "views": [{"name": n, "description": v.description} for n, v in spec.views.items()],
        "enums": list(spec.enums.keys()),
    }


def describe_entity(spec: Spec, entity: str) -> dict[str, Any]:
    if entity not in spec.entities:
        raise KeyError(f"unknown entity {entity!r}")
    e = spec.entities[entity]
    fields = [
        {
            "name": "id",
            "type": "uuid",
            "required": True,
            "description": "Primary key (auto-generated).",
            "system": True,
        }
    ]
    for fname, fld in e.fields.items():
        fields.append(
            {
                "name": fname,
                "type": fld.type.value,
                "enum": fld.enum,
                "required": fld.required,
                "unique": fld.unique,
                "default": fld.default,
                "description": fld.description,
            }
        )
    for fname in ("created_at", "updated_at"):
        fields.append(
            {
                "name": fname,
                "type": "timestamptz",
                "required": True,
                "description": f"Auto-managed {fname.replace('_', ' ')} timestamp.",
                "system": True,
            }
        )
    relationships = [
        {
            "name": rname,
            "kind": rel.kind,
            "target": rel.target,
            "required": rel.required,
            "column": f"{rname}_id" if rel.kind == "belongs_to" else None,
            "description": rel.description,
        }
        for rname, rel in e.relationships.items()
    ]
    return {
        "entity": entity,
        "description": e.description,
        "fields": fields,
        "relationships": relationships,
        "checks": [{"name": c.name, "expr": c.expr, "description": c.description} for c in e.checks],
        "unique": [{"name": u.name, "fields": u.fields} for u in e.unique],
        "indexes": [{"name": i.name, "fields": i.fields, "unique": i.unique} for i in e.indexes],
        "exclusions": [
            {"name": x.name, "description": x.description} for x in e.exclusions
        ],
        "crud_examples": _crud_examples(entity, e),
    }


def describe_operation(spec: Spec, operation: str) -> dict[str, Any]:
    if operation not in spec.operations:
        raise KeyError(f"unknown operation {operation!r}")
    o = spec.operations[operation]
    inputs = {
        iname: {
            "type": inp.type.value,
            "enum": inp.enum,
            "required": inp.required,
            "description": inp.description,
        }
        for iname, inp in o.inputs.items()
    }
    example = {
        iname: f"<{inp.type.value}>" for iname, inp in o.inputs.items() if inp.required
    }
    import json

    return {
        "operation": operation,
        "description": o.description,
        "inputs": inputs,
        "entities": o.entities,
        "example": f"dbmachine do {operation} --json '{json.dumps(example)}'",
    }


def _crud_examples(entity: str, e) -> dict[str, str]:
    import json

    sample = {}
    for fname, fld in e.fields.items():
        if fld.required and fld.default is None:
            sample[fname] = f"<{fld.type.value}>"
    for rname, rel in e.relationships.items():
        if rel.kind == "belongs_to" and rel.required:
            sample[f"{rname}_id"] = "<uuid>"
    payload = json.dumps(sample)
    return {
        "create": f"dbmachine create {entity} --json '{payload}'",
        "list": f"dbmachine list {entity}",
        "get": f"dbmachine get {entity} <id>",
        "update": f"dbmachine update {entity} <id> --json '{{...}}'",
    }
