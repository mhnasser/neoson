import asyncio
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from pillow_heif import register_heif_opener
from test_document_extraction import Env

from neoson_core.memory.document_jobs import DocumentExtractor
from neoson_core.memory.document_search import SqliteDocumentSearch
from neoson_core.memory.documents import DocumentRejected


def picture(kind: str = "PNG", *, metadata: bool = False) -> bytes:
    image = Image.new("RGB", (12, 8), "white")
    output = io.BytesIO()
    if kind == "HEIF":
        register_heif_opener()
    options: dict[str, Any] = {}
    if metadata:
        exif = Image.Exif()
        exif[274] = 6
        exif[34853] = {1: "N", 3: "E"}
        options["exif"] = exif
    image.save(output, format=kind, **options)
    return output.getvalue()


def command(root: Path, confidence: float = 90) -> tuple[str, ...]:
    program = root / "fake_ocr.py"
    program.write_text(
        "import os,sys\n"
        "from PIL import Image\n"
        "assert sys.argv[-4:] == ['stdout','-l','por','tsv']\n"
        "assert not any(k.startswith('NEOSON_') for k in os.environ)\n"
        "assert not Image.open(sys.argv[1]).getexif()\n"
        "print('level\\tconf\\ttext')\n"
        f"print('5\\t{confidence}\\tDocumento Atlas aprovado para teste')\n",
        encoding="utf-8",
    )
    return sys.executable, str(program)


@pytest.mark.parametrize(
    ("kind", "extension"), [("PNG", "png"), ("JPEG", "jpg"), ("HEIF", "heic")]
)
def test_images_are_normalized_locally_and_ocr_has_confidence_and_source(
    tmp_path: Path, kind: str, extension: str
) -> None:
    env = Env(tmp_path, tesseract_command=command(tmp_path))
    source = tmp_path / f"foto.{extension}"
    raw = picture(kind)
    source.write_bytes(raw)
    doc = env.store.ingest(source, extract=True)
    assert doc.id == hashlib.sha256(raw).hexdigest()
    stored = tmp_path / "originals" / f"{doc.id}.png"
    assert stored.read_bytes().startswith(b"\x89PNG")
    assert not env.store.ingest(source, extract=True).created
    assert DocumentExtractor(env.store).process_once()
    assert env.rows("SELECT route, confidence, needs_ocr FROM document_pages") == [
        ("ocr", 90.0, 0)
    ]
    assert env.rows("SELECT status FROM document_extraction_jobs") == [("completed",)]
    hits = asyncio.run(SqliteDocumentSearch(env.db).search("Atlas", 1))
    assert hits[0].page == 1
    assert env.rows("SELECT stored_sha256 FROM documents") == [
        (hashlib.sha256(stored.read_bytes()).hexdigest(),)
    ]
    assert env.store.forget(doc.id) and not stored.exists()
    with pytest.raises(DocumentRejected, match="esquecido"):
        env.store.ingest(source, extract=True)


def test_orientation_and_location_metadata_are_removed_before_storage(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path, tesseract_command=command(tmp_path))
    source = tmp_path / "foto.jpg"
    raw = picture("JPEG", metadata=True)
    source.write_bytes(raw)
    assert Image.open(io.BytesIO(raw)).getexif().get(34853)
    doc = env.store.ingest(source, extract=False)
    stored = tmp_path / "originals" / f"{doc.id}.png"
    with Image.open(stored) as clean:
        assert clean.size == (8, 12)
        assert not clean.getexif() and not clean.info
    assert len(list((tmp_path / "originals").iterdir())) == 1


@pytest.mark.parametrize("failure", ["size", "pixels", "signature", "corrupt"])
def test_invalid_photos_leave_no_original_or_record(
    tmp_path: Path, failure: str
) -> None:
    limits = (
        {"image_max_bytes": 16}
        if failure == "size"
        else {"max_pixels": 1}
        if failure == "pixels"
        else {}
    )
    env = Env(tmp_path, **limits)
    source = tmp_path / "foto.png"
    raw = (
        picture()
        if failure in {"size", "pixels"}
        else b"wrong"
        if failure == "signature"
        else b"\x89PNG\r\n\x1a\nbroken"
    )
    source.write_bytes(raw)
    with pytest.raises(DocumentRejected):
        env.store.ingest(source, extract=False)
    assert not env.rows("SELECT * FROM documents")
    assert not (tmp_path / "originals").exists()


def test_low_confidence_is_visible_and_never_uses_a_cloud_provider(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path, tesseract_command=command(tmp_path, 20))
    source = tmp_path / "foto.png"
    source.write_bytes(picture())
    doc = env.store.ingest(source, extract=False)
    assert env.extractor.process_once()
    assert env.rows("SELECT confidence, needs_ocr FROM document_pages") == [(20.0, 1)]
    quality = json.loads(env.rows("SELECT quality_json FROM documents")[0][0])
    assert quality["low_confidence_pages"] == 1 and quality["ocr_pages"] == 1
    assert '"ocr_confidence_min": 20.0' in doc.note.read_text(encoding="utf-8")


def test_missing_ocr_dependency_is_a_recorded_failure(tmp_path: Path) -> None:
    env = Env(tmp_path, tesseract_command=(str(tmp_path / "missing-ocr"),))
    source = tmp_path / "foto.png"
    source.write_bytes(picture())
    doc = env.store.ingest(source, extract=False)
    assert env.extractor.process_once()
    assert env.rows("SELECT status,last_error FROM document_extraction_jobs") == [
        ("failed", "ocr_dependency")
    ]
    assert not doc.note.exists()


def test_scanned_pdf_uses_local_ocr(tmp_path: Path) -> None:
    env = Env(tmp_path, tesseract_command=command(tmp_path))
    image = Image.new("RGB", (100, 50), "white")
    output = io.BytesIO()
    image.save(output, format="PDF")
    doc = env.ingest(output.getvalue())
    assert env.extractor.process_once()
    assert env.rows("SELECT route,confidence FROM document_pages") == [("ocr", 90.0)]
    assert "Documento Atlas aprovado" in doc.note.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("program", "error"),
    [
        ("import time; time.sleep(30)", "timeout"),
        ("print('invalid TSV')", "ocr_failure"),
        ("import sys; sys.exit(3)", "ocr_failure"),
    ],
)
def test_ocr_process_failures_are_contained(
    tmp_path: Path, program: str, error: str
) -> None:
    env = Env(
        tmp_path,
        tesseract_command=(sys.executable, "-c", program),
        ocr_timeout_seconds=1,
    )
    source = tmp_path / "foto.png"
    source.write_bytes(picture())
    doc = env.store.ingest(source, extract=False)
    assert env.extractor.process_once()
    status = "pending" if error == "timeout" else "failed"
    assert env.rows("SELECT status,last_error FROM document_extraction_jobs") == [
        (status, error)
    ]
    assert not doc.note.exists()


def test_failed_ocr_can_be_explicitly_retried_after_fix(tmp_path: Path) -> None:
    env = Env(tmp_path, tesseract_command=(str(tmp_path / "missing-ocr"),))
    source = tmp_path / "foto.png"
    source.write_bytes(picture())
    doc = env.store.ingest(source, extract=False)
    assert env.extractor.process_once()
    fixed = Env(tmp_path, tesseract_command=command(tmp_path))
    assert fixed.extractor.retry(doc.id)
    assert fixed.extractor.process_once()
    assert not fixed.extractor.retry(doc.id)
    assert fixed.store.forget(doc.id)
    assert not fixed.extractor.retry(doc.id)
