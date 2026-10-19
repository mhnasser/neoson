"""Schemas de transporte compartilhados entre o gateway e os clientes do NeoSon."""

from importlib.metadata import version

__version__ = version("neoson-contracts")
import neoson_core  # noqa: F401  (violação proposital para testar a proteção)
