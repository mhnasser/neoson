import argparse
import sys
from collections.abc import Sequence
from uuid import UUID

import httpx2
from pydantic import ValidationError

from neoson_robot.client import GatewayClient, GatewayError
from neoson_robot.config import INVALID_GATEWAY_URL, gateway_url

TIMEOUT_SECONDS = 30.0


def main(argv: Sequence[str] | None = None, http: httpx2.Client | None = None) -> int:
    """Envia uma mensagem ao gateway e mostra a resposta. `http` é usado nos testes."""
    parser = argparse.ArgumentParser(
        prog="neoson-robot",
        description="Envia uma mensagem ao gateway do NeoSon e mostra a resposta.",
    )
    parser.add_argument("text", help="texto da mensagem")
    parser.add_argument("--gateway-url", help="padrão: NEOSON_GATEWAY_URL ou local")
    parser.add_argument("--session-id", type=UUID, help="continua uma sessão")
    args = parser.parse_args(argv)

    try:
        url = gateway_url(args.gateway_url)
    except ValueError as error:
        parser.error(str(error))

    try:
        client = http or httpx2.Client(base_url=url, timeout=TIMEOUT_SECONDS)
    except httpx2.InvalidURL:
        parser.error(INVALID_GATEWAY_URL)

    try:
        reply = GatewayClient(client).send(args.text, args.session_id)
    except ValidationError as error:
        problems = "; ".join(detail["msg"] for detail in error.errors())
        print(f"Mensagem inválida: {problems}", file=sys.stderr)
        return 2
    except GatewayError as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1
    finally:
        if http is None:
            client.close()

    print(reply.text)
    return 0
