"""Continuidade entre reinícios sem guardar áudio, texto ou token."""

import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid4


class FileSessionState:
    def __init__(self, path: Path, gateway_url: str) -> None:
        self.path = path
        self.gateway = hashlib.sha256(gateway_url.encode()).hexdigest()

    def load(self) -> UUID | None:
        if not self.path.exists():
            return None
        try:
            with self.path.open("rb") as source:
                raw = source.read(1025)
            if len(raw) > 1024:
                raise ValueError
            data = json.loads(raw)
            if (
                set(data) != {"version", "gateway", "session_id"}
                or data["version"] != 1
                or data["gateway"] != self.gateway
            ):
                raise ValueError
            return UUID(data["session_id"])
        except (ValueError, TypeError, KeyError, OSError):
            raise ValueError("Estado local de sessão inválido.") from None

    def save(self, session_id: UUID) -> None:
        text = json.dumps(
            {"version": 1, "gateway": self.gateway, "session_id": str(session_id)},
            sort_keys=True,
        )
        if self.path.exists() and self.path.read_text(encoding="utf-8") == text:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            "." + self.path.name + "." + uuid4().hex + ".tmp"
        )
        try:
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
