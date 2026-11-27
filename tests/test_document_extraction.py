import asyncio
import importlib.util
import io
import os
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)

from neoson_core.infrastructure import db
from neoson_core.memory import document_jobs
from neoson_core.memory.document_jobs import DocumentExtractor, parse
from neoson_core.memory.document_search import SqliteDocumentSearch
from neoson_core.memory.documents import DocumentLimits, DocumentRejected, DocumentStore
from neoson_worker import docs_main


def pdf(*texts: str, encrypted: bool = False) -> bytes:
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=600, height=800)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): writer._add_object(font)}
                )
            }
        )
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 20 700 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(content)
    if encrypted:
        writer.encrypt("dummy-password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class Env:
    def __init__(self, root: Path, **limits: Any):
        self.now = datetime(2026, 11, 27, 12, tzinfo=UTC)
        self.root = root
        self.db = root / "data.db"
        self.store = DocumentStore(
            self.db,
            root / "originals",
            root / "vault",
            DocumentLimits(**limits),
            lambda: self.now,
        )
        self.extractor = DocumentExtractor(self.store)

    def ingest(self, data: bytes):  # type: ignore[no-untyped-def]
        source = self.root / "entrada.pdf"
        source.write_bytes(data)
        return self.store.ingest(source, extract=True)

    def rows(self, sql: str) -> list[tuple[Any, ...]]:
        with closing(db.connect(self.db)) as c:
            return [tuple(row) for row in c.execute(sql)]


def test_pdf_queue_extraction_search_page_and_forget(tmp_path: Path) -> None:
    env = Env(tmp_path)
    data = pdf("Plano Atlas", "", "O Atlas foi aprovado")
    first = env.ingest(data)
    assert first.created and first.extraction_state == "pending"
    assert not first.note.exists() and first.chunks == 0
    assert not env.ingest(data).created
    assert env.extractor.process_once()
    assert not env.extractor.process_once()
    assert env.rows("SELECT page, route FROM document_pages ORDER BY page") == [
        (1, "text"),
        (2, "empty"),
        (3, "text"),
    ]
    assert env.rows("SELECT status, attempts FROM document_extraction_jobs") == [
        ("completed", 1)
    ]
    assert len(env.rows("SELECT * FROM graph_jobs")) == 2
    assert env.rows("SELECT page, needs_ocr FROM document_pages ORDER BY page") == [
        (1, 1),
        (2, 1),
        (3, 0),
    ]
    note = first.note.read_text(encoding="utf-8")
    assert "```text" in note and '"empty_pages": 1' in note
    hits = asyncio.run(SqliteDocumentSearch(env.db).search("aprovado", 5))
    assert len(hits) == 1 and hits[0].page == 3
    assert env.store.forget(first.id)
    assert asyncio.run(SqliteDocumentSearch(env.db).search("Atlas", 5)) == []
    with pytest.raises(DocumentRejected, match="esquecido"):
        env.ingest(data)


@pytest.mark.parametrize(
    ("data", "limits", "error"),
    [
        (b"%PDF-broken", {}, "invalid"),
        (pdf("secret", encrypted=True), {}, "encrypted"),
        (pdf("one", "two"), {"max_pages": 1}, "page_limit"),
        (pdf("long text"), {"max_chars": 2}, "character_limit"),
    ],
)
def test_bad_pdf_is_recorded_without_partial_content(
    tmp_path: Path, data: bytes, limits: dict[str, Any], error: str
) -> None:
    env = Env(tmp_path, **limits)
    first = env.ingest(data)
    assert env.extractor.process_once()
    assert env.rows("SELECT status, last_error FROM document_extraction_jobs") == [
        ("failed", error)
    ]
    assert env.rows("SELECT COUNT(*) FROM document_chunks") == [(0,)]
    assert not first.note.exists()


def test_pdf_size_signature_and_optional_dependency(
    tmp_path: Path, monkeypatch: Any
) -> None:
    env = Env(tmp_path, pdf_max_bytes=100)
    with pytest.raises(DocumentRejected, match="tamanho"):
        env.ingest(pdf("Atlas"))
    with pytest.raises(DocumentRejected, match="assinatura"):
        env.ingest(b"not a pdf")
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with pytest.raises(DocumentRejected, match="extra opcional"):
        env.ingest(b"%PDF-")
    assert env.rows("SELECT COUNT(*) FROM documents") == [(0,)]


def test_timeout_is_retried_with_wait_and_attempt_limit(
    tmp_path: Path, monkeypatch: Any
) -> None:
    env = Env(tmp_path)
    env.ingest(pdf("Atlas"))

    def timeout(*args: Any) -> None:
        raise DocumentRejected("timeout")

    monkeypatch.setattr(document_jobs, "parse", timeout)
    for attempt in range(1, 4):
        assert env.extractor.process_once()
        assert not env.extractor.process_once()
        assert env.rows("SELECT attempts FROM document_extraction_jobs") == [(attempt,)]
        env.now += timedelta(seconds=61)
    assert env.rows("SELECT status FROM document_extraction_jobs") == [("failed",)]


def test_lease_blocks_second_worker_and_forgotten_source_cannot_commit(
    tmp_path: Path, monkeypatch: Any
) -> None:
    env = Env(tmp_path)
    doc = env.ingest(pdf("Atlas"))

    def forget(data: bytes, limits: DocumentLimits) -> list[dict[str, object]]:
        assert not DocumentExtractor(env.store).process_once()
        assert env.store.forget(doc.id)
        return [{"page": 1, "text": "Atlas", "route": "text"}]

    monkeypatch.setattr(document_jobs, "parse", forget)
    assert env.extractor.process_once()
    assert env.rows("SELECT status FROM document_extraction_jobs") == [("stale",)]
    assert not doc.note.exists() and not env.rows("SELECT * FROM document_chunks")


def test_expired_lease_is_reclaimed_and_original_changes_are_refused(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    doc = env.ingest(pdf("Atlas"))
    with closing(db.connect(env.db)) as c:
        c.execute(
            "UPDATE document_extraction_jobs SET status='leased',"
            " lease_id='old', lease_until=?",
            ((env.now - timedelta(seconds=1)).isoformat(),),
        )
    (tmp_path / "originals" / f"{doc.id}.pdf").write_bytes(b"changed")
    assert env.extractor.process_once()
    assert env.rows("SELECT last_error FROM document_extraction_jobs") == [
        ("source_changed",)
    ]


def test_parser_process_gets_no_credentials_or_project_working_directory(
    monkeypatch: Any,
) -> None:
    original = subprocess.Popen
    monkeypatch.setenv("NEOSON_API_TOKEN", "dummy-token")

    def spawn(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        assert "NEOSON_API_TOKEN" not in kwargs["env"]
        assert kwargs["cwd"] != str(Path.cwd())
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    assert parse(pdf("Atlas"), DocumentLimits())[0]["text"].strip() == "Atlas"


def test_migration_preserves_old_documents_chunks_graph_and_fts(
    tmp_path: Path, monkeypatch: Any
) -> None:
    all_migrations = db.migrations()
    monkeypatch.setattr(db, "migrations", lambda: all_migrations[:21])
    env = Env(tmp_path)
    source = tmp_path / "old.txt"
    source.write_text("Atlas antigo", encoding="utf-8")
    doc = env.store.ingest(source, extract=True)
    before = env.rows("SELECT rowid, document_id, n, text FROM document_chunks")
    monkeypatch.setattr(db, "migrations", lambda: all_migrations)
    with closing(db.connect(env.db)) as c:
        assert db.migrate(c) == [22]
        assert not c.execute("PRAGMA foreign_key_check").fetchall()
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert env.rows("SELECT rowid, document_id, n, text FROM document_chunks") == before
    assert env.rows("SELECT document_id FROM graph_jobs") == [(doc.id,)]
    assert asyncio.run(SqliteDocumentSearch(env.db).search("Atlas", 1))[0].page is None


def test_failed_rebuild_rolls_back_and_restores_foreign_keys(
    tmp_path: Path, monkeypatch: Any
) -> None:
    env = Env(tmp_path)
    original = db.migrations()
    broken = db.Migration(
        23,
        "0023_invalid.sql",
        "-- foreign_keys: off\n"
        "INSERT INTO document_chunks(document_id,n,text) VALUES ('missing',0,'bad');",
    )
    monkeypatch.setattr(db, "migrations", lambda: [*original, broken])
    with closing(db.connect(env.db)) as c:
        with pytest.raises(RuntimeError, match="referências"):
            db.migrate(c)
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not c.execute("SELECT * FROM document_chunks").fetchall()
        assert db.schema_version(c) == 22


def test_parser_deadline_terminates_its_process(monkeypatch: Any) -> None:
    original = subprocess.Popen
    children: list[Any] = []

    def slow(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        child = original(
            [sys.executable, "-c", "import time; time.sleep(30)"], **kwargs
        )
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", slow)
    with pytest.raises(DocumentRejected, match="timeout"):
        parse(b"%PDF-", DocumentLimits(extract_timeout_seconds=1))
    assert children[0].poll() is not None


def test_cli_queues_and_processes_pdf_without_printing_content(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NEOSON_DB_PATH", str(tmp_path / "data.db"))
    monkeypatch.setenv("NEOSON_DOCUMENTS_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("NEOSON_VAULT_PATH", str(tmp_path / "vault"))
    source = tmp_path / "entrada.pdf"
    source.write_bytes(pdf("PRIVATE_ATLAS"))
    assert docs_main.main(["ingest", str(source)]) == 0
    assert "pending" in capsys.readouterr().out
    assert docs_main.main(["process", "--once"]) == 0
    assert docs_main.main(["list"]) == 0
    output = capsys.readouterr().out
    assert "completed" in output and "PRIVATE_ATLAS" not in output


def test_retry_wait_starts_after_slow_parser_failure(
    tmp_path: Path, monkeypatch: Any
) -> None:
    env = Env(tmp_path)
    env.ingest(pdf("Atlas"))

    def timeout(*args: Any) -> None:
        env.now += timedelta(seconds=60)
        raise DocumentRejected("timeout")

    monkeypatch.setattr(document_jobs, "parse", timeout)
    assert env.extractor.process_once()
    assert not env.extractor.process_once()


def test_pdf_signature_cannot_bypass_parser_with_text_extension(tmp_path: Path) -> None:
    env = Env(tmp_path)
    source = tmp_path / "fake.txt"
    source.write_bytes(pdf("Atlas"))
    with pytest.raises(DocumentRejected, match="extensão"):
        env.store.ingest(source, extract=False)
    assert not env.rows("SELECT * FROM documents")
