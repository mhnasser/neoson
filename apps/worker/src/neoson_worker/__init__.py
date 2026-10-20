"""Worker do NeoSon: consome a outbox com lease, retry e idempotência."""

from importlib.metadata import version

__version__ = version("neoson-worker")
