"""Persistência de concessões; nenhum efeito externo dentro da transação SQLite."""

import json
import math
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from neoson_core.engine.actions import (
    ActionConflict,
    ActionNotFound,
    ActionView,
    PendingAction,
    checksum,
)
from neoson_core.engine.messages import ToolCall
from neoson_core.engine.persistence import OutboxEvent
from neoson_core.engine.tools import RiskLevel
from neoson_core.infrastructure import outbox
from neoson_core.infrastructure.db import connect, migrate, now_iso, transaction


def insert(connection: sqlite3.Connection, action: PendingAction) -> None:
    text = action.serialize()
    connection.execute(
        "INSERT INTO pending_actions "
        "(id,run_id,session_id,owner,payload,checksum,expires_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            str(action.id),
            str(action.run_id),
            str(action.session_id),
            action.owner,
            text,
            checksum(text),
            action.expires_at.isoformat(),
            now_iso(),
        ),
    )


def _decode(row: sqlite3.Row) -> ActionView:
    try:
        if (
            (
                row["state"]
                in ("approved", "executing", "completed", "unknown", "invalid")
                and row["decision"] != "approve"
            )
            or (row["state"] == "denied" and row["decision"] != "deny")
            or (row["state"] == "pending" and row["decision"] is not None)
        ):
            raise ValueError
        if checksum(row["payload"]) != row["checksum"]:
            raise ValueError
        data = json.loads(row["payload"])
        call = data["call"]
        skill = data["skill"]
        if skill is not None and (
            not isinstance(skill, list)
            or len(skill) != 3
            or not all(isinstance(s, str) and s for s in skill)
        ):
            raise ValueError
        timeout = data["timeout_seconds"]
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError
        if not isinstance(data["owner"], str) or not data["owner"]:
            raise ValueError
        if not isinstance(data["context"], str) or len(data["context"]) != 64:
            raise ValueError
        if not isinstance(call["id"], str) or not isinstance(call["name"], str):
            raise ValueError
        if not isinstance(call["arguments"], dict):
            raise ValueError
        action = PendingAction(
            UUID(data["id"]),
            UUID(data["run_id"]),
            UUID(data["session_id"]),
            data["owner"],
            ToolCall(call["id"], call["name"], call["arguments"]),
            data["context"],
            datetime.fromisoformat(data["expires_at"]),
            timeout,
            RiskLevel(data["risk"]),
            None if skill is None else tuple(skill),
        )
        if (
            action.expires_at.utcoffset() is None
            or (
                str(action.id),
                str(action.run_id),
                str(action.session_id),
                action.owner,
                action.expires_at.isoformat(),
            )
            != (
                row["id"],
                row["run_id"],
                row["session_id"],
                row["owner"],
                row["expires_at"],
            )
            or action.serialize() != row["payload"]
        ):
            raise ValueError
        return ActionView(action, row["state"], row["decision"])
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RecursionError,
        OverflowError,
    ):
        raise ActionConflict("Registro de autorização inválido.") from None


def _get(c: sqlite3.Connection, action_id: UUID, owner: str) -> ActionView:
    row = c.execute(
        "SELECT * FROM pending_actions WHERE id=? AND owner=?", (str(action_id), owner)
    ).fetchone()
    if row is None:
        raise ActionNotFound
    view = _decode(row)
    if view.state in ("pending", "approved") and view.action.expires_at <= datetime.now(
        UTC
    ):
        c.execute(
            "UPDATE pending_actions SET state='expired',updated_at=? WHERE id=?",
            (now_iso(), str(action_id)),
        )
        return ActionView(view.action, "expired", view.decision)
    return view


def _event(c: sqlite3.Connection, action: PendingAction, state: str) -> None:
    outbox.insert(
        c,
        OutboxEvent(
            type="neoson.action." + state,
            run_id=action.run_id,
            session_id=action.session_id,
            payload={"action_id": str(action.id)},
        ),
    )


class SqliteActionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        with closing(connect(path)) as c:
            migrate(c)

    def get(self, action_id: UUID, owner: str) -> ActionView:
        with closing(connect(self.path)) as c, transaction(c):
            return _get(c, action_id, owner)

    def for_run(self, run_id: UUID, owner: str) -> tuple[ActionView, ...]:
        with closing(connect(self.path)) as c, transaction(c):
            ids = c.execute(
                "SELECT id FROM pending_actions WHERE run_id=? AND owner=? "
                "ORDER BY id LIMIT 64",
                (str(run_id), owner),
            ).fetchall()
            return tuple(_get(c, UUID(r[0]), owner) for r in ids)

    def decide(self, action_id: UUID, owner: str, approve: bool) -> ActionView:
        decision = "approve" if approve else "deny"
        with closing(connect(self.path)) as c, transaction(c):
            view = _get(c, action_id, owner)
            if view.decision == decision:
                return view
            if view.state != "pending":
                raise ActionConflict("Ação não aceita esta decisão.")
            state = "approved" if approve else "denied"
            c.execute(
                "UPDATE pending_actions SET state=?,decision=?,updated_at=? WHERE id=?",
                (state, decision, now_iso(), str(action_id)),
            )
            _event(c, view.action, state)
            return ActionView(view.action, state, decision)

    def claim(self, action: PendingAction) -> bool:
        with closing(connect(self.path)) as c, transaction(c):
            view = _get(c, action.id, action.owner)
            if (
                view.state != "approved"
                or view.action != action
                or view.action.serialize() != action.serialize()
            ):
                return False
            changed = c.execute(
                "UPDATE pending_actions SET state='executing',updated_at=? "
                "WHERE id=? AND state='approved'",
                (now_iso(), str(action.id)),
            ).rowcount
            if changed:
                _event(c, action, "executing")
            return changed == 1

    def finish(self, action: PendingAction, state: str) -> None:
        if state not in ("invalid", "unknown", "completed"):
            raise ValueError("Estado de ação inválido.")
        expected = "approved" if state == "invalid" else "executing"
        with closing(connect(self.path)) as c, transaction(c):
            view = _get(c, action.id, action.owner)
            if view.state != expected or view.action.serialize() != action.serialize():
                raise ActionConflict("Ação indisponível para confirmação.")
            c.execute(
                "UPDATE pending_actions SET state=?,updated_at=? WHERE id=?",
                (state, now_iso(), str(action.id)),
            )
            _event(c, action, state)
