"""Fila local de extração com lease e finalização condicionada à reserva atual."""

import base64
import hashlib
import importlib.util
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import asdict
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from neoson_core.infrastructure.db import transaction
from neoson_core.memory.documents import (
    NOTES_DIR,
    DocumentLimits,
    DocumentRejected,
    DocumentStore,
    IngestResult,
    _safe_name,
    _write_private,
    normalize,
    slug,
    split_chunks,
)
from neoson_core.telemetry.vault import write_atomic


def register_binary(store: DocumentStore, path: Path, *, extract: bool) -> IngestResult:
    extension = path.suffix.lower().lstrip(".")
    is_pdf = extension == "pdf"
    if importlib.util.find_spec("pypdf" if is_pdf else "PIL") is None:
        raise DocumentRejected("Este formato requer o extra opcional documents.")
    limit = store.limits.pdf_max_bytes if is_pdf else store.limits.image_max_bytes
    try:
        with path.open("rb") as source:
            data = source.read(limit + 1)
    except OSError:
        raise DocumentRejected("Não foi possível ler o arquivo.") from None
    if len(data) > limit:
        raise DocumentRejected("Arquivo acima do limite de tamanho.")
    valid = (
        data.startswith(b"%PDF-")
        if is_pdf
        else (
            (extension in {"jpg", "jpeg"} and data.startswith(b"\xff\xd8\xff"))
            or (extension == "png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
            or (extension == "heic" and data[4:8] == b"ftyp")
        )
    )
    if not valid:
        raise DocumentRejected("Tipo ou assinatura do arquivo inválidos.")
    document_id = hashlib.sha256(data).hexdigest()
    if not is_pdf:
        result = _run_parser(data, store.limits, kind="image", operation="prepare")
        try:
            data = base64.b64decode(result["image"], validate=True)
        except (ValueError, TypeError, KeyError):
            raise DocumentRejected("invalid_output") from None
        if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > limit:
            raise DocumentRejected("image_limit")
        extension = "png"
    title = _safe_name(path.stem)
    note = NOTES_DIR / f"{slug(title)}-{document_id[:8]}.md"
    now = store._clock().astimezone(UTC).isoformat()
    with store._connection() as c, transaction(c):
        old = c.execute(
            "SELECT * FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if old:
            if old["status"] != "active":
                raise DocumentRejected("Este documento foi esquecido antes.")
            count = c.execute(
                "SELECT COUNT(*) FROM document_chunks WHERE document_id = ?",
                (document_id,),
            ).fetchone()[0]
            return IngestResult(
                document_id,
                old["title"],
                count,
                False,
                store._vault / old["note_path"],
                old["extraction_state"],
            )
        _write_private(store._store / f"{document_id}.{extension}", data)
        c.execute(
            "INSERT INTO documents (id, original_name, extension, size_bytes, "
            "chars, title, note_path, ingested_at, extraction_state, stored_sha256) "
            "VALUES "
            "(?,?,?,?,0,?,?,?,'pending',?)",
            (
                document_id,
                _safe_name(path.name),
                extension,
                len(data),
                title,
                note.as_posix(),
                now,
                hashlib.sha256(data).hexdigest(),
            ),
        )
        c.execute(
            "INSERT INTO document_extraction_jobs (document_id, extract_graph, "
            "created_at) VALUES (?,?,?)",
            (document_id, int(extract), now),
        )
    return IngestResult(document_id, title, 0, True, store._vault / note, "pending")


def _run_parser(
    data: bytes,
    limits: DocumentLimits,
    *,
    kind: str = "pdf",
    operation: str = "extract",
) -> dict[str, Any]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "LANG"}
    }
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    request = json.dumps(
        {
            "data": base64.b64encode(data).decode("ascii"),
            "limits": asdict(limits),
            "kind": kind,
            "operation": operation,
        }
    ).encode()
    with tempfile.TemporaryDirectory(prefix="neoson-parser-") as scratch:
        process = subprocess.Popen(
            [sys.executable, "-m", "neoson_core.memory.document_parser"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            cwd=scratch,
            start_new_session=os.name == "posix",
        )
        try:
            output, _ = process.communicate(
                request, timeout=limits.extract_timeout_seconds
            )
        except subprocess.TimeoutExpired:
            if sys.platform != "win32":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.communicate()
            raise DocumentRejected("timeout") from None
    maximum = (
        limits.image_max_bytes * 2 + 100_000
        if operation == "prepare"
        else limits.max_chars * 12 + 100_000
    )
    if process.returncode or len(output) > maximum:
        raise DocumentRejected("parser_failure")
    try:
        result = json.loads(output)
        if result.get("error"):
            raise DocumentRejected(result["error"])
        if not isinstance(result, dict):
            raise ValueError
        return result
    except DocumentRejected:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise DocumentRejected("invalid_output") from None


def parse(
    data: bytes, limits: DocumentLimits, *, kind: str = "pdf"
) -> list[dict[str, Any]]:
    result = _run_parser(data, limits, kind=kind)
    try:
        pages = result["pages"]
        if not isinstance(pages, list) or not 1 <= len(pages) <= limits.max_pages:
            raise ValueError
        chars = 0
        for number, page in enumerate(pages, 1):
            if (
                page["page"] != number
                or page["route"] not in {"text", "empty", "ocr"}
                or not isinstance(page["text"], str)
            ):
                raise ValueError
            page["text"] = normalize(page["text"])
            confidence = page.get("confidence")
            if confidence is not None and (
                not isinstance(confidence, (int, float))
                or not math.isfinite(confidence)
                or not 0 <= confidence <= 100
            ):
                raise ValueError
            chars += len(page["text"])
        if chars > limits.max_chars:
            raise ValueError
        return pages
    except DocumentRejected:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise DocumentRejected("invalid_output") from None


class DocumentExtractor:
    def __init__(self, store: DocumentStore) -> None:
        self.store = store

    def retry(self, document_id: str) -> bool:
        with self.store._connection() as c, transaction(c):
            changed = c.execute(
                "UPDATE document_extraction_jobs SET status='pending', attempts=0, "
                "lease_id=NULL, lease_until=NULL, retry_at=NULL, last_error=NULL "
                "WHERE document_id=? AND status='failed' AND EXISTS "
                "(SELECT 1 FROM documents WHERE id=? AND status='active')",
                (document_id, document_id),
            ).rowcount
            if changed:
                c.execute(
                    "UPDATE documents SET extraction_state='pending' WHERE id=?",
                    (document_id,),
                )
        return bool(changed)

    def process_once(self) -> bool:
        store = self.store
        now = store._clock().astimezone(UTC)
        token = str(uuid4())
        with store._connection() as c, transaction(c):
            c.execute(
                "UPDATE document_extraction_jobs SET status='failed', "
                "last_error='attempt_limit' WHERE attempts >= 3 AND "
                "(status='pending' OR (status='leased' AND lease_until <= ?))",
                (now.isoformat(),),
            )
            c.execute(
                "UPDATE documents SET extraction_state='failed' WHERE id IN (SELECT"
                " document_id FROM document_extraction_jobs WHERE status='failed')"
            )
            if c.execute(
                "SELECT 1 FROM document_extraction_jobs WHERE status='leased' AND "
                "lease_until > ?",
                (now.isoformat(),),
            ).fetchone():
                return False
            job = c.execute(
                "SELECT j.*, d.extension, d.size_bytes, d.title, d.note_path, "
                "d.original_name, d.ingested_at, d.stored_sha256 "
                "FROM document_extraction_jobs j "
                "JOIN documents d ON d.id=j.document_id WHERE d.status='active' AND"
                " ((j.status='pending' AND (j.retry_at IS NULL OR j.retry_at <= ?))"
                " OR (j.status='leased' AND j.lease_until <= ?)) ORDER BY "
                "j.created_at, j.document_id LIMIT 1",
                (now.isoformat(), now.isoformat()),
            ).fetchone()
            if job is None:
                return False
            c.execute(
                "UPDATE document_extraction_jobs SET status='leased', "
                "attempts=attempts+1, lease_id=?, lease_until=? WHERE document_id=?",
                (
                    token,
                    (
                        now
                        + timedelta(seconds=store.limits.extract_timeout_seconds + 30)
                    ).isoformat(),
                    job["document_id"],
                ),
            )
        document_id = job["document_id"]
        pages: list[dict[str, Any]] = []
        error: str | None = None
        try:
            with (store._store / f"{document_id}.{job['extension']}").open(
                "rb"
            ) as source:
                data = source.read(
                    max(store.limits.pdf_max_bytes, store.limits.image_max_bytes) + 1
                )
            if len(data) != job["size_bytes"] or hashlib.sha256(data).hexdigest() != (
                job["stored_sha256"] or document_id
            ):
                raise DocumentRejected("source_changed")
            pages = (
                parse(data, store.limits)
                if job["extension"] == "pdf"
                else parse(data, store.limits, kind="image")
            )
        except DocumentRejected as rejected:
            error = (
                str(rejected)
                if str(rejected)
                in {
                    "dependency",
                    "encrypted",
                    "page_limit",
                    "character_limit",
                    "timeout",
                    "invalid",
                    "invalid_output",
                    "parser_failure",
                    "source_changed",
                    "ocr_dependency",
                    "ocr_failure",
                    "pixels",
                    "image_limit",
                    "image_type",
                    "image_frames",
                }
                else "parser_failure"
            )
        except OSError:
            error = "source_unavailable"
        with store._connection() as c, transaction(c):
            owned = c.execute(
                "SELECT j.attempts FROM document_extraction_jobs j JOIN documents d"
                " ON d.id=j.document_id WHERE j.document_id=? AND j.lease_id=? AND "
                "j.status='leased' AND j.lease_until > ? AND d.status='active'",
                (document_id, token, store._clock().astimezone(UTC).isoformat()),
            ).fetchone()
            if owned is None:
                return True
            if error:
                retry = error == "timeout" and owned["attempts"] < 3
                c.execute(
                    "UPDATE document_extraction_jobs SET status=?, last_error=?, "
                    "lease_id=NULL, retry_at=? WHERE document_id=?",
                    (
                        "pending" if retry else "failed",
                        error,
                        (
                            store._clock().astimezone(UTC) + timedelta(seconds=60)
                        ).isoformat(),
                        document_id,
                    ),
                )
                c.execute(
                    "UPDATE documents SET extraction_state=? WHERE id=?",
                    ("pending" if retry else "failed", document_id),
                )
                return True
            quality = {
                "pages": len(pages),
                "text_pages": sum(p["route"] == "text" for p in pages),
                "empty_pages": sum(p["route"] == "empty" for p in pages),
                "short_pages": sum(0 < len(p["text"]) < 20 for p in pages),
                "ocr_pages": sum(p["route"] == "ocr" for p in pages),
                "low_confidence_pages": sum(
                    p.get("confidence") is not None
                    and p["confidence"] < store.limits.ocr_min_confidence
                    for p in pages
                ),
                "unrendered_pages": sum(
                    p.get("warning") == "no_embedded_image" for p in pages
                ),
            }
            scores = [p["confidence"] for p in pages if p.get("confidence") is not None]
            if scores:
                quality["ocr_confidence_mean"] = sum(scores) / len(scores)
                quality["ocr_confidence_min"] = min(scores)
            chunks = [
                (p["page"], chunk)
                for p in pages
                for chunk in split_chunks(p["text"], store.limits.chunk_chars)
            ]
            text = "\n\n".join(
                f"Página {p['page']} ({p['route']})\n{p['text']}" for p in pages
            )
            note = store._render(
                document_id,
                job["title"],
                job["original_name"],
                job["ingested_at"],
                text,
                len(chunks),
            )
            note += "\nQualidade: " + json.dumps(quality, ensure_ascii=True) + "\n"
            write_atomic(store._vault / job["note_path"], note)
            c.executemany(
                "INSERT INTO document_pages (document_id, page, route, chars, "
                "needs_ocr, confidence, warning_code) "
                "VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        document_id,
                        p["page"],
                        p["route"],
                        len(p["text"]),
                        int(
                            len(p["text"]) < 20
                            or (
                                p.get("confidence") is not None
                                and p["confidence"] < store.limits.ocr_min_confidence
                            )
                        ),
                        p.get("confidence"),
                        "no_embedded_image"
                        if p.get("warning") == "no_embedded_image"
                        else None,
                    )
                    for p in pages
                ],
            )
            c.executemany(
                "INSERT INTO document_chunks (document_id, n, text, page) VALUES "
                "(?,?,?,?)",
                [
                    (document_id, n, chunk, page)
                    for n, (page, chunk) in enumerate(chunks)
                ],
            )
            if job["extract_graph"]:
                c.executemany(
                    "INSERT INTO graph_jobs (id, kind, source_id, document_id, "
                    "chunk, created_at) VALUES (?,'extract',?,?,?,?) ON CONFLICT "
                    "(source_id, kind) DO NOTHING",
                    [
                        (
                            str(uuid4()),
                            f"document:{document_id}:{n}",
                            document_id,
                            n,
                            now.isoformat(),
                        )
                        for n in range(len(chunks))
                    ],
                )
            c.execute(
                "UPDATE documents SET chars=?, quality_json=?, "
                "extraction_state='completed' WHERE id=?",
                (sum(len(p["text"]) for p in pages), json.dumps(quality), document_id),
            )
            c.execute(
                "UPDATE document_extraction_jobs SET status='completed', "
                "lease_id=NULL, last_error=NULL WHERE document_id=?",
                (document_id,),
            )
        return True
