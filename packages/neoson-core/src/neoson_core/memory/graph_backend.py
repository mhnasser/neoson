"""Escolha explícita do armazenamento do grafo; SQLite é o padrão."""

from neoson_core.engine.graph import GraphStore
from neoson_core.infrastructure.config import ConfigError, ServerSettings
from neoson_core.memory.graph import SqliteGraphStore
from neoson_core.memory.neo4j_graph import GraphBackendError, open_neo4j


def graph_store(settings: ServerSettings) -> GraphStore:
    if settings.graph_backend == "sqlite":
        return SqliteGraphStore(settings.db_path)
    if not settings.neo4j_uri or settings.neo4j_password is None:
        raise ConfigError(
            "NEOSON_NEO4J_URI e NEOSON_NEO4J_PASSWORD são obrigatórios com "
            "NEOSON_GRAPH_BACKEND=neo4j."
        )
    try:
        return open_neo4j(
            settings.neo4j_uri,
            settings.neo4j_user,
            settings.neo4j_password.get_secret_value(),
            settings.neo4j_database,
            settings.neo4j_timeout_seconds,
        )
    except GraphBackendError as error:
        raise ConfigError(str(error)) from None
