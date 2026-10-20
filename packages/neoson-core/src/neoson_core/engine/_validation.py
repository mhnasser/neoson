from collections.abc import Mapping
from types import MappingProxyType


def require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} não pode ser vazio.")


def frozen_mapping(mapping: Mapping[str, object]) -> Mapping[str, object]:
    """Cópia somente leitura (rasa): alterar o original não muda o objeto criado."""
    return MappingProxyType(dict(mapping))
