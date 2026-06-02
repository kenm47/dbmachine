"""Custom operations for the bookings example app."""

from __future__ import annotations

import sqlalchemy as sa


def confirm_appointment(ctx, *, appointment_id: str):
    """Move a pending appointment to 'confirmed'."""
    row = ctx.conn.execute(
        sa.text(
            "UPDATE appointment SET status = 'confirmed' "
            "WHERE id = :id RETURNING id, status"
        ),
        {"id": appointment_id},
    ).first()
    if row is None:
        raise ValueError(f"appointment {appointment_id} not found")
    cust = ctx.conn.execute(
        sa.text(
            "SELECT c.email FROM appointment a JOIN customer c ON c.id = a.customer_id "
            "WHERE a.id = :id"
        ),
        {"id": appointment_id},
    ).scalar()
    if cust:
        ctx.actions.email(cust, "Booking confirmed", "Your appointment is confirmed.")
    return {"id": str(row.id), "status": row.status}


def cancel_appointment(ctx, *, appointment_id: str, reason: str | None = None):
    """Cancel an appointment, freeing its time window."""
    note = f"Cancelled: {reason}" if reason else "Cancelled"
    row = ctx.conn.execute(
        sa.text(
            "UPDATE appointment SET status = 'cancelled', "
            "notes = COALESCE(notes || ' | ', '') || :note "
            "WHERE id = :id RETURNING id, status"
        ),
        {"id": appointment_id, "note": note},
    ).first()
    if row is None:
        raise ValueError(f"appointment {appointment_id} not found")
    return {"id": str(row.id), "status": row.status}
