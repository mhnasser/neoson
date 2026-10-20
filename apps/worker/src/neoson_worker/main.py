import argparse
import logging
import os
import signal
import socket
import threading
from collections.abc import Sequence

from neoson_core.infrastructure.config import ConfigError, load_settings
from neoson_core.telemetry import usage
from neoson_worker.worker import Handler, RetryPolicy, Worker


def build_handlers() -> dict[str, list[Handler]]:
    record = Handler(usage.HANDLER_NAME, usage.record_usage)
    return {event_type: [record] for event_type in usage.EVENT_TYPES}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neoson-worker", description="Consome os eventos da outbox do NeoSon."
    )
    parser.add_argument(
        "--once", action="store_true", help="processa o que estiver pronto e sai"
    )
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as error:
        raise SystemExit(str(error)) from None
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    worker = Worker(
        db_path=settings.db_path,
        handlers=build_handlers(),
        worker_id=f"{socket.gethostname()}:{os.getpid()}",
        lease_seconds=settings.worker_lease_seconds,
        retry=RetryPolicy(max_attempts=settings.worker_max_attempts),
    )
    if args.once:
        processed = worker.run_once()
        logging.getLogger("neoson_worker").info("%d evento(s) reservado(s)", processed)
        return 0

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    worker.run_forever(stop, settings.worker_poll_seconds)
    return 0
