import uvicorn

from neoson_gateway.app import create_app

# Só a máquina local até existir configuração e autenticação.
HOST = "127.0.0.1"
PORT = 8000


def main() -> None:
    uvicorn.run(create_app(), host=HOST, port=PORT)
