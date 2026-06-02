"""Custom operations for the CRM example app."""

from __future__ import annotations

import sqlalchemy as sa


def win_deal(ctx, *, deal_id: str):
    """Mark a deal as won and stamp the close time."""
    row = ctx.conn.execute(
        sa.text(
            "UPDATE deal SET stage = 'won', closed_at = now() "
            "WHERE id = :id RETURNING id, title, amount, stage"
        ),
        {"id": deal_id},
    ).first()
    if row is None:
        raise ValueError(f"deal {deal_id} not found")
    return {
        "id": str(row.id),
        "title": row.title,
        "amount": float(row.amount),
        "stage": row.stage,
    }
