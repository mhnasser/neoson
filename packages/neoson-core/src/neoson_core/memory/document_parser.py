"""Parser filho: recebe bytes/limites; devolve páginas, nunca acessa banco/vault."""

import base64
import csv
import io
import json
import math
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Any


def clean_image(data: bytes, limits: dict[str, Any]) -> Any:
    from PIL import Image, ImageOps

    if data[4:8] == b"ftyp":
        from pillow_heif import register_heif_opener

        register_heif_opener()
    Image.MAX_IMAGE_PIXELS = limits["max_pixels"]
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as source:
            if source.format not in {"JPEG", "PNG", "HEIF"}:
                raise ValueError("image_type")
            if source.width * source.height > limits["max_pixels"]:
                raise ValueError("pixels")
            if getattr(source, "n_frames", 1) != 1:
                raise ValueError("image_frames")
            oriented = ImageOps.exif_transpose(source).convert("RGBA")
            clean = Image.new("RGB", oriented.size, "white")
            clean.paste(oriented, mask=oriented.getchannel("A"))
            return clean


def ocr(image: Any, limits: dict[str, Any]) -> tuple[str, float]:
    with tempfile.TemporaryDirectory(prefix="neoson-ocr-") as scratch:
        path = Path(scratch) / "page.png"
        image.save(path, format="PNG")
        try:
            result = subprocess.run(
                [*limits["tesseract_command"], str(path), "stdout", "-l", "por", "tsv"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=limits["ocr_timeout_seconds"],
                check=False,
            )
        except FileNotFoundError:
            raise ValueError("ocr_dependency") from None
        except subprocess.TimeoutExpired:
            raise ValueError("timeout") from None
    if result.returncode or len(result.stdout) > limits["max_chars"] * 64 + 100_000:
        raise ValueError("ocr_failure")
    words: list[str] = []
    confidence: list[float] = []
    reader = csv.DictReader(io.StringIO(result.stdout.decode("utf-8")), delimiter="\t")
    if not {"level", "conf", "text"} <= set(reader.fieldnames or []):
        raise ValueError("ocr_failure")
    for row in reader:
        if row.get("level") != "5" or not row.get("text", "").strip():
            continue
        score = float(row["conf"])
        if not math.isfinite(score) or not 0 <= score <= 100:
            raise ValueError("ocr_failure")
        words.append(row["text"].strip())
        confidence.append(score)
    text = " ".join(words)
    if len(text) > limits["max_chars"]:
        raise ValueError("character_limit")
    return text, sum(confidence) / len(confidence) if confidence else 0


def extract_pdf(data: bytes, limits: dict[str, Any]) -> list[dict[str, object]]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise ValueError("encrypted")
    if not 1 <= len(reader.pages) <= limits["max_pages"]:
        raise ValueError("page_limit")
    pages: list[dict[str, object]] = []
    chars = 0
    for number, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        route = "text" if text.strip() else "empty"
        confidence = None
        warning = None
        if len(text.strip()) < 20:
            images = page.images
            if len(images) > 32:
                raise ValueError("image_limit")
            if images:
                parts = [text] if text.strip() else []
                scores = []
                for image in images:
                    content, score = ocr(clean_image(image.data, limits), limits)
                    parts.append(content)
                    scores.append(score)
                text = "\n".join(parts)
                confidence = min(scores)
                route = "ocr" if text.strip() else "empty"
            else:
                warning = "no_embedded_image"
        chars += len(text)
        if chars > limits["max_chars"]:
            raise ValueError("character_limit")
        pages.append(
            {
                "page": number,
                "text": text,
                "route": route,
                "confidence": confidence,
                "warning": warning,
            }
        )
    return pages


def main() -> None:
    try:
        request = json.loads(sys.stdin.buffer.read())
        limits = request["limits"]
        if sys.platform == "linux":
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
            cpu = math.ceil(limits["extract_timeout_seconds"]) + 1
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))

        def deny_access(event: str, args: tuple[object, ...]) -> None:
            if event in {"socket.connect", "socket.bind", "sqlite3.connect"}:
                raise PermissionError("parser_access")

        sys.addaudithook(deny_access)
        data = base64.b64decode(request["data"], validate=True)
        result: dict[str, object]
        if request.get("kind") == "image":
            image = clean_image(data, limits)
            if request.get("operation") == "prepare":
                output = io.BytesIO()
                image.save(output, format="PNG")
                if output.tell() > limits["image_max_bytes"]:
                    raise ValueError("image_limit")
                result = {"image": base64.b64encode(output.getvalue()).decode("ascii")}
            else:
                text, confidence = ocr(image, limits)
                result = {
                    "pages": [
                        {
                            "page": 1,
                            "text": text,
                            "route": "ocr" if text else "empty",
                            "confidence": confidence,
                            "warning": None,
                        }
                    ]
                }
        else:
            result = {"pages": extract_pdf(data, limits)}
    except ModuleNotFoundError:
        result = {"error": "dependency"}
    except ValueError as error:
        code = str(error)
        result = {
            "error": code
            if code
            in {
                "encrypted",
                "page_limit",
                "character_limit",
                "pixels",
                "image_type",
                "image_frames",
                "image_limit",
                "ocr_dependency",
                "ocr_failure",
                "timeout",
            }
            else "invalid"
        }
    except Exception:
        result = {"error": "invalid"}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
