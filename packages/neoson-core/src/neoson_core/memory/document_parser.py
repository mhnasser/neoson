"""Parser filho: recebe bytes/limites; devolve páginas, nunca acessa banco/vault."""

import base64
import io
import json
import math
import sys
from typing import Any


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
        chars += len(text)
        if chars > limits["max_chars"]:
            raise ValueError("character_limit")
        pages.append(
            {"page": number, "text": text, "route": "text" if text.strip() else "empty"}
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
        pages = extract_pdf(base64.b64decode(request["data"], validate=True), limits)
        result: dict[str, object] = {"pages": pages}
    except ModuleNotFoundError:
        result = {"error": "dependency"}
    except ValueError as error:
        code = str(error)
        result = {
            "error": code
            if code in {"encrypted", "page_limit", "character_limit"}
            else "invalid"
        }
    except Exception:
        result = {"error": "invalid"}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
