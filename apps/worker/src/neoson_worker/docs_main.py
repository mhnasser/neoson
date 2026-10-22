"""Ingestão local de documentos de texto; mostra só IDs, títulos e contagens."""

import argparse
from collections.abc import Sequence
from pathlib import Path

from neoson_core.infrastructure.config import ConfigError, ServerSettings, load_settings
from neoson_core.memory.documents import DocumentLimits, DocumentRejected, DocumentStore


def store_for(settings: ServerSettings) -> DocumentStore:
    return DocumentStore(
        settings.db_path,
        settings.documents_path,
        settings.vault_path,
        DocumentLimits(
            max_bytes=settings.document_max_bytes,
            max_chars=settings.document_max_chars,
            chunk_chars=settings.document_chunk_chars,
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neoson-docs")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="ingere um arquivo .md ou .txt")
    ingest.add_argument("path", type=Path)
    commands.add_parser("list", help="lista os documentos ingeridos")
    forget = commands.add_parser("forget", help="esquece um documento pelo ID")
    forget.add_argument("id")
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
        store = store_for(settings)
    except ConfigError as error:
        raise SystemExit(str(error)) from None
    if args.command == "ingest":
        try:
            # Fatos só com o grafo ligado; o worker de extração os processa depois.
            result = store.ingest(args.path, extract=settings.memory_graph_enabled)
        except DocumentRejected as error:
            raise SystemExit(f"Documento recusado: {error}") from None
        state = "ingerido" if result.created else "já existia"
        print(f"{result.id} | {state} | {result.chunks} trecho(s) | {result.title}")
        if result.created and not settings.memory_graph_enabled:
            print("Grafo desligado: nenhum fato será extraído deste documento.")
        return 0
    if args.command == "list":
        documents = store.list()
        if not documents:
            print("Nenhum documento.")
        for doc in documents:
            print(
                f"{doc.id} | {doc.status} | {doc.chars} caracteres | "
                f"{doc.chunks} trecho(s) | {doc.ingested_at[:10]} | {doc.title}"
            )
        return 0
    if not store.forget(args.id):
        print("Documento não encontrado ou já esquecido.")
        return 1
    print(f"{args.id} | esquecido")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
