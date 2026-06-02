"""Side-effect abstraction for custom operations (email / SMS / payment).

v1 ships stubs only: every action is recorded to ``dbm_actions_log`` inside the
operation's transaction, so a dry, deterministic backend can be built and tested
without real third-party credentials. Real adapters are a drop-in later — they
implement the same :class:`Actions` surface.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class Actions:
    """Handle passed to custom operations for performing side effects."""

    conn: object  # SQLAlchemy connection (transactional)
    performed: list[dict] = field(default_factory=list)

    def _record(self, kind: str, payload: dict) -> dict:
        import sqlalchemy as sa

        rec = {"kind": kind, **payload}
        self.conn.execute(
            sa.text(
                "INSERT INTO dbm_actions_log(kind, payload) VALUES (:k, CAST(:p AS jsonb))"
            ),
            {"k": kind, "p": json.dumps(payload)},
        )
        self.performed.append(rec)
        return rec

    def email(self, to: str, subject: str, body: str) -> dict:
        return self._record("email", {"to": to, "subject": subject, "body": body})

    def sms(self, to: str, message: str) -> dict:
        return self._record("sms", {"to": to, "message": message})

    def charge(self, customer: str, amount_cents: int, currency: str = "usd") -> dict:
        return self._record(
            "charge",
            {"customer": customer, "amount_cents": amount_cents, "currency": currency},
        )


ACTIONS_LOG_DDL = """\
CREATE TABLE IF NOT EXISTS dbm_actions_log (
  id bigserial PRIMARY KEY,
  at timestamptz NOT NULL DEFAULT now(),
  kind text NOT NULL,
  payload jsonb NOT NULL
);"""
