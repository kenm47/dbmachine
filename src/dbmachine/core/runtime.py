"""Operations runtime: auto-CRUD, custom operations, query, audit, import.

Everything runs inside transactions. Writes are recorded to ``dbm_audit_log``
via per-row triggers — except bulk import, which sets ``dbm.bulk='on'`` to
bypass the per-row trigger (one audit row per imported row kills performance and
risks transaction timeouts at scale) and instead writes a single summary record.
"""

from __future__ import annotations

import datetime as _dt
import json
import uuid as _uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from .actions import Actions
from .compiler import build_metadata
from .config import Project
from .db import engine_for
from .ops import OpContext, resolve_implementation
from .spec import Spec
from .validation import (
    entity_input_model,
    operation_input_model,
    validate,
)


class RuntimeError_(Exception):
    """Operational error with a stable, agent-readable message."""


def _jsonable(value: Any) -> Any:
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).decode("utf-8", "replace")
    return value


def _row_to_dict(row) -> dict:
    return {k: _jsonable(v) for k, v in dict(row._mapping).items()}


@dataclass
class Runtime:
    project: Project
    spec: Spec

    def __post_init__(self) -> None:
        self.metadata, _ = build_metadata(self.spec)
        self.engine = engine_for(self.project)

    def _table(self, entity: str) -> sa.Table:
        if entity not in self.spec.entities:
            raise RuntimeError_(f"unknown entity {entity!r}")
        return self.metadata.tables[entity]

    # -- CRUD --------------------------------------------------------------

    def create(self, entity: str, data: dict) -> dict:
        table = self._table(entity)
        clean = validate(entity_input_model(self.spec, entity, partial=False), data)
        with self.engine.begin() as conn:
            row = conn.execute(
                sa.insert(table).values(**clean).returning(*table.c)
            ).one()
            return _row_to_dict(row)

    def get(self, entity: str, row_id: str) -> dict | None:
        table = self._table(entity)
        with self.engine.connect() as conn:
            res = conn.execute(
                sa.select(table).where(table.c.id == row_id)
            ).first()
            return _row_to_dict(res) if res else None

    def list(
        self,
        entity: str,
        *,
        filters: dict | None = None,
        limit: int = 100,
        offset: int = 0,
        order_by: str | None = None,
        descending: bool = False,
    ) -> list[dict]:
        table = self._table(entity)
        stmt = sa.select(table)
        for col, val in (filters or {}).items():
            if col not in table.c:
                raise RuntimeError_(f"{entity!r} has no column {col!r} to filter on")
            stmt = stmt.where(table.c[col] == val)
        if order_by:
            if order_by not in table.c:
                raise RuntimeError_(f"{entity!r} has no column {order_by!r} to order by")
            col = table.c[order_by]
            stmt = stmt.order_by(col.desc() if descending else col.asc())
        stmt = stmt.limit(limit).offset(offset)
        with self.engine.connect() as conn:
            return [_row_to_dict(r) for r in conn.execute(stmt)]

    def update(self, entity: str, row_id: str, data: dict) -> dict:
        table = self._table(entity)
        clean = validate(entity_input_model(self.spec, entity, partial=True), data)
        if not clean:
            raise RuntimeError_("update requires at least one field to change")
        with self.engine.begin() as conn:
            row = conn.execute(
                sa.update(table)
                .where(table.c.id == row_id)
                .values(**clean)
                .returning(*table.c)
            ).first()
            if row is None:
                raise RuntimeError_(f"{entity} {row_id!r} not found")
            return _row_to_dict(row)

    def delete(self, entity: str, row_id: str) -> dict:
        table = self._table(entity)
        with self.engine.begin() as conn:
            res = conn.execute(sa.delete(table).where(table.c.id == row_id))
            if res.rowcount == 0:
                raise RuntimeError_(f"{entity} {row_id!r} not found")
        return {"deleted": row_id, "entity": entity}

    # -- custom operations -------------------------------------------------

    def do(self, operation: str, inputs: dict) -> dict:
        if operation not in self.spec.operations:
            raise RuntimeError_(f"unknown operation {operation!r}")
        opdef = self.spec.operations[operation]
        clean = validate(operation_input_model(self.spec, operation), inputs)
        if not opdef.implementation:
            raise RuntimeError_(
                f"operation {operation!r} has no implementation registered"
            )
        fn = resolve_implementation(self.project.root, opdef.implementation)
        with self.engine.begin() as conn:
            actions = Actions(conn=conn)
            ctx = OpContext(conn=conn, actions=actions, operation=operation)
            result = fn(ctx, **clean)
        return {
            "operation": operation,
            "result": _coerce_result(result),
            "actions": actions.performed,
        }

    # -- query -------------------------------------------------------------

    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        stripped = sql.strip().rstrip(";").lstrip()
        head = stripped.split(None, 1)[0].lower() if stripped else ""
        if head not in ("select", "with", "table"):
            raise RuntimeError_(
                "query only accepts read-only statements (SELECT/WITH/TABLE). "
                "Use `do` or the CRUD commands to write."
            )
        with self.engine.connect() as conn:
            conn.execute(sa.text("SET TRANSACTION READ ONLY"))
            rows = conn.execute(sa.text(stripped), params or {})
            return [_row_to_dict(r) for r in rows]

    # -- audit -------------------------------------------------------------

    def audit(self, *, entity: str | None = None, limit: int = 50) -> list[dict]:
        clauses = ""
        params: dict = {"lim": limit}
        if entity:
            clauses = "WHERE entity = :entity"
            params["entity"] = entity
        sql = (
            f"SELECT id, at, actor, entity, action, row_id, changes "
            f"FROM dbm_audit_log {clauses} ORDER BY id DESC LIMIT :lim"
        )
        with self.engine.connect() as conn:
            return [_row_to_dict(r) for r in conn.execute(sa.text(sql), params)]

    # -- bulk import -------------------------------------------------------

    def import_rows(
        self,
        entity: str,
        rows: list[dict],
        *,
        dedup_on: list[str] | None = None,
    ) -> dict:
        """Bulk-insert validated rows in one transaction.

        Per-row audit triggers are bypassed (``dbm.bulk='on'``); a single
        summary row is written to ``dbm_audit_log`` instead.
        """
        table = self._table(entity)
        model = entity_input_model(self.spec, entity, partial=False)

        inserted = 0
        skipped = 0
        errors: list[dict] = []
        clean_rows: list[dict] = []
        for i, raw in enumerate(rows):
            try:
                clean_rows.append(validate(model, raw))
            except Exception as e:  # collect, don't abort the whole import
                errors.append({"row": i, "error": str(e)})

        with self.engine.begin() as conn:
            conn.execute(sa.text("SET LOCAL dbm.bulk = 'on'"))
            from sqlalchemy.dialects.postgresql import insert as _pg_insert

            for clean in clean_rows:
                if dedup_on:
                    # RETURNING is empty when the row was skipped by ON CONFLICT —
                    # a reliable inserted/skipped signal (rowcount is not portable).
                    stmt = (
                        _pg_insert(table)
                        .values(**clean)
                        .on_conflict_do_nothing(index_elements=dedup_on)
                        .returning(table.c.id)
                    )
                    if conn.execute(stmt).first() is not None:
                        inserted += 1
                    else:
                        skipped += 1
                else:
                    conn.execute(sa.insert(table).values(**clean))
                    inserted += 1
            # single summary audit record
            conn.execute(
                sa.text(
                    "INSERT INTO dbm_audit_log(entity, action, changes) "
                    "VALUES (:e, 'IMPORT', CAST(:c AS jsonb))"
                ),
                {
                    "e": entity,
                    "c": json.dumps(
                        {"inserted": inserted, "skipped": skipped, "errors": len(errors)}
                    ),
                },
            )
        return {
            "entity": entity,
            "received": len(rows),
            "inserted": inserted,
            "skipped": skipped,
            "errors": errors,
        }


def _coerce_result(result: Any) -> Any:
    if result is None:
        return None
    if isinstance(result, (str, int, float, bool, list, dict)):
        return result
    if hasattr(result, "_mapping"):
        return _row_to_dict(result)
    return _jsonable(result)
