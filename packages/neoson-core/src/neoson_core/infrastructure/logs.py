"""Configuração única de logs dos processos do servidor (§18).

`text` mantém o formato de antes; `json` escreve um objeto por linha. Nos dois, padrões
de segredo são mascarados antes da escrita. Os módulos registram IDs e estados, nunca
conteúdo de conversa: a redação é uma segunda barreira, não a primeira.
"""

import json
import logging
import logging.handlers
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
MASK = "[redigido]"
_SECRETS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    # Token de bot do Telegram, inclusive colado em ".../bot<token>/" na URL da API.
    re.compile(r"(?<![0-9])\d{6,12}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"(?i)(password|senha|api[_-]?key|token)=[^\s&]+"),
)
# Bibliotecas HTTP registram URLs em INFO; algumas URLs carregam tokens.
QUIET = ("httpx2", "httpcore2", "openai", "neo4j")


def redact(text: str) -> str:
    for pattern in _SECRETS:
        text = pattern.sub(MASK, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class JsonFormatter(logging.Formatter):
    """Um objeto por linha: momento UTC, nível, logger, processo, mensagem e,
    quando o registro trouxer, `run_id`, tipo e traceback da exceção (redigidos)."""

    def __init__(self, process: str) -> None:
        super().__init__()
        self._process = process

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "process": self._process,
            "message": redact(record.getMessage()),
        }
        run_id = getattr(record, "run_id", None)
        if run_id is not None:
            entry["run_id"] = str(run_id)
        if record.exc_info and record.exc_info[0] is not None:
            entry["error"] = record.exc_info[0].__name__
            entry["traceback"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(
    level: str,
    process: str,
    *,
    fmt: Literal["text", "json"] = "text",
    file: Path | None = None,
    max_bytes: int = 5_000_000,
    backups: int = 5,
) -> None:
    """Substitui os handlers do logger raiz; pode ser chamada de novo (testes)."""
    formatter: logging.Formatter = (
        JsonFormatter(process) if fmt == "json" else RedactingFormatter(TEXT_FORMAT)
    )
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if file is not None:
        file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
            )
        )
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(level)
    for name in QUIET:
        logging.getLogger(name).setLevel(max(logging.WARNING, root.level))
