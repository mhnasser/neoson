"""Ingestão de documentos de texto (§9.3, D11): original preservado, nota e trechos.

O conteúdo do documento é dado não confiável. Ele nunca vira instrução nem código
executável no vault: a nota mostra o texto dentro de um bloco de código mais longo que
qualquer sequência de crases do próprio texto, o que neutraliza blocos `dataviewjs`,
links e embeds do Obsidian.
"""

import hashlib
import os
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from neoson_core.infrastructure.db import connect, migrate, transaction
from neoson_core.telemetry.vault import frontmatter, write_atomic

EXTENSIONS = {".md": "md", ".markdown": "markdown", ".txt": "txt"}
NOTES_DIR = Path("Documentos")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TITLE_UNSAFE = re.compile(r"[^\w\s.,;:()'\"?¿¡\-–—]")


class DocumentRejected(ValueError):
    """Motivo fixo, sem conteúdo do arquivo nem caminho."""


@dataclass(frozen=True, slots=True)
class DocumentLimits:
    max_bytes: int = 2_000_000
    max_chars: int = 200_000
    chunk_chars: int = 3000

    def __post_init__(self) -> None:
        if min(self.max_bytes, self.max_chars, self.chunk_chars) < 1:
            raise ValueError("Limites de documento precisam ser positivos.")


@dataclass(frozen=True, slots=True)
class IngestResult:
    id: str
    title: str
    chunks: int
    created: bool
    note: Path


@dataclass(frozen=True, slots=True)
class DocumentInfo:
    id: str
    title: str
    status: str
    chars: int
    chunks: int
    ingested_at: str


def normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    lines = [line.rstrip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def read_text(data: bytes) -> str:
    if b"\x00" in data:
        raise DocumentRejected("Arquivo binário não é aceito.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise DocumentRejected("Use texto em UTF-8.") from None
    return normalize(text)


def title_of(text: str, fallback: str) -> str:
    for line in text.split("\n"):
        clean = line.strip().lstrip("#").strip()
        if clean:
            break
    else:
        clean = fallback
    safe = " ".join(_TITLE_UNSAFE.sub("", clean).split())[:120]
    return safe or "Documento"


def split_chunks(text: str, size: int) -> list[str]:
    """Trechos de até `size` caracteres, preferindo quebras de parágrafo e espaço."""
    pieces: list[str] = []
    for paragraph in text.split("\n\n"):
        while len(paragraph) > size:
            cut = paragraph.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            pieces.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        if paragraph:
            pieces.append(paragraph)
    chunks: list[str] = []
    for piece in pieces:
        if chunks and len(chunks[-1]) + 2 + len(piece) <= size:
            chunks[-1] += "\n\n" + piece
        else:
            chunks.append(piece)
    return chunks


def fenced(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def slug(title: str) -> str:
    ascii_title = (
        unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    )
    value = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")[:50].strip("-")
    return value or "documento"


class DocumentStore:
    def __init__(
        self,
        db_path: Path,
        store_dir: Path,
        vault: Path,
        limits: DocumentLimits | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db, self._store, self._vault = db_path, store_dir, vault
        self.limits = limits or DocumentLimits()
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._connection() as c:
            migrate(c)

    def ingest(self, path: Path, *, extract: bool) -> IngestResult:
        extension = EXTENSIONS.get(path.suffix.lower())
        if extension is None:
            raise DocumentRejected("Use um arquivo .md, .markdown ou .txt.")
        try:
            with path.open("rb") as source:
                data = source.read(self.limits.max_bytes + 1)
        except OSError:
            raise DocumentRejected("Não foi possível ler o arquivo.") from None
        if not data:
            raise DocumentRejected("Arquivo vazio.")
        if len(data) > self.limits.max_bytes:
            raise DocumentRejected("Arquivo acima do limite de tamanho.")
        text = read_text(data)
        if not text:
            raise DocumentRejected("O arquivo não tem texto.")
        if len(text) > self.limits.max_chars:
            raise DocumentRejected("Texto acima do limite de caracteres.")
        document_id = hashlib.sha256(data).hexdigest()
        with self._connection() as c:
            row = c.execute(
                "SELECT title, status, note_path FROM documents WHERE id = ?",
                (document_id,),
            ).fetchone()
        if row is not None:
            if row["status"] != "active":
                raise DocumentRejected("Este documento foi esquecido antes.")
            count = self._count(document_id)
            return IngestResult(
                document_id, row["title"], count, False, self._vault / row["note_path"]
            )
        title = title_of(text, path.stem)
        chunks = split_chunks(text, self.limits.chunk_chars)
        now = self._clock().astimezone(UTC).isoformat()
        note = NOTES_DIR / f"{slug(title)}-{document_id[:8]}.md"
        # Arquivos antes do banco: se o banco falhar, reingerir sobrescreve igual.
        original = self._store / f"{document_id}.{extension}"
        _write_private(original, data)
        write_atomic(
            self._vault / note,
            self._render(document_id, title, path.name, now, text, len(chunks)),
        )
        with self._connection() as c, transaction(c):
            c.execute(
                "INSERT INTO documents (id, original_name, extension, size_bytes,"
                " chars, title, note_path, ingested_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    document_id,
                    _safe_name(path.name),
                    extension,
                    len(data),
                    len(text),
                    title,
                    note.as_posix(),
                    now,
                ),
            )
            c.executemany(
                "INSERT INTO document_chunks (document_id, n, text) VALUES (?, ?, ?)",
                [(document_id, n, chunk) for n, chunk in enumerate(chunks)],
            )
            if extract:
                c.executemany(
                    "INSERT INTO graph_jobs (id, kind, source_id, document_id, chunk,"
                    " created_at) VALUES (?, 'extract', ?, ?, ?, ?)"
                    " ON CONFLICT (source_id, kind) DO NOTHING",
                    [
                        (
                            str(uuid4()),
                            f"document:{document_id}:{n}",
                            document_id,
                            n,
                            now,
                        )
                        for n in range(len(chunks))
                    ],
                )
        return IngestResult(document_id, title, len(chunks), True, self._vault / note)

    def list(self) -> list[DocumentInfo]:
        with self._connection() as c:
            rows = c.execute(
                "SELECT d.id, d.title, d.status, d.chars, d.ingested_at,"
                " (SELECT COUNT(*) FROM document_chunks k WHERE k.document_id = d.id)"
                " AS chunks FROM documents d ORDER BY d.ingested_at DESC, d.id"
            ).fetchall()
        return [
            DocumentInfo(
                r["id"],
                r["title"],
                r["status"],
                r["chars"],
                r["chunks"],
                r["ingested_at"],
            )
            for r in rows
        ]

    def forget(self, document_id: str) -> bool:
        """Retira os fatos dos trechos e apaga original, trechos e nota."""
        now = self._clock().astimezone(UTC).isoformat()
        with self._connection() as c, transaction(c):
            row = c.execute(
                "SELECT extension, note_path FROM documents"
                " WHERE id = ? AND status = ?",
                (document_id, "active"),
            ).fetchone()
            if row is None:
                return False
            count = c.execute(
                "SELECT COUNT(*) FROM document_chunks WHERE document_id = ?",
                (document_id,),
            ).fetchone()[0]
            c.execute(
                "UPDATE graph_jobs SET status = 'stale',"
                " last_error = 'source_forgotten'"
                " WHERE document_id = ? AND kind = 'extract'"
                " AND status IN ('pending', 'leased')",
                (document_id,),
            )
            c.executemany(
                "INSERT INTO graph_jobs (id, kind, source_id, document_id, chunk,"
                " created_at) VALUES (?, 'retract', ?, ?, ?, ?)"
                " ON CONFLICT (source_id, kind) DO NOTHING",
                [
                    (str(uuid4()), f"document:{document_id}:{n}", document_id, n, now)
                    for n in range(count)
                ],
            )
            c.execute(
                "DELETE FROM document_chunks WHERE document_id = ?", (document_id,)
            )
            c.execute(
                "UPDATE documents SET status = 'forgotten', forgotten_at = ?"
                " WHERE id = ?",
                (now, document_id),
            )
        (self._store / f"{document_id}.{row['extension']}").unlink(missing_ok=True)
        (self._vault / row["note_path"]).unlink(missing_ok=True)
        return True

    def _count(self, document_id: str) -> int:
        with self._connection() as c:
            return int(
                c.execute(
                    "SELECT COUNT(*) FROM document_chunks WHERE document_id = ?",
                    (document_id,),
                ).fetchone()[0]
            )

    def _render(
        self, document_id: str, title: str, name: str, now: str, text: str, chunks: int
    ) -> str:
        head = frontmatter(
            {
                "tipo": "neoson-documento",
                "fonte": f"document:{document_id}",
                "sha256": document_id,
                "titulo": title,
                "nome_original": _safe_name(name),
                "ingerido_em": now,
                "caracteres": len(text),
                "trechos": chunks,
            }
        )
        return (
            f"{head}\n# {title}\n\n"
            "Conteúdo importado: dado não confiável, mostrado só como texto.\n\n"
            f"{fenced(text)}\n"
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = connect(self._db)
        try:
            yield connection
        finally:
            connection.close()


def _safe_name(name: str) -> str:
    return " ".join(_TITLE_UNSAFE.sub("", name).split())[:200] or "documento"


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
