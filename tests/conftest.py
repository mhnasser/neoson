import logging
from collections.abc import Iterator

import pytest

from neoson_core.infrastructure.logs import QUIET


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """Os `main()` dos processos configuram o logger raiz; nada vaza entre testes."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    quiet = {name: logging.getLogger(name).level for name in QUIET}
    yield
    for handler in root.handlers[:]:
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)
    for name, previous in quiet.items():
        logging.getLogger(name).setLevel(previous)
