from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient

from neoson_core.engine import EchoEngine, Engine, RunResult, UserMessage
from neoson_gateway.app import create_app
from neoson_robot.client import GatewayClient, GatewayError
from neoson_robot.config import DEFAULT_GATEWAY_URL, gateway_url
from neoson_robot.main import main

TOKEN = "token-de-teste"


@pytest.fixture(autouse=True)
def robot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEOSON_API_TOKEN", TOKEN)


class RecordingEngine(EchoEngine):
    def __init__(self) -> None:
        self.received: list[UserMessage] = []

    async def run(self, message: UserMessage) -> RunResult:
        self.received.append(message)
        return await super().run(message)


class BrokenEngine:
    async def run(self, message: UserMessage) -> RunResult:
        raise RuntimeError("falha interna")


def gateway(engine: Engine | None = None) -> TestClient:
    """Gateway real em processo, sem rede."""
    return TestClient(
        create_app(engine or EchoEngine(), TOKEN),
        raise_server_exceptions=False,
    )


def mock_http(handler: httpx2.MockTransport) -> httpx2.Client:
    return httpx2.Client(base_url="http://gateway.test", transport=handler)


# --- cliente ---


def test_client_talks_to_the_real_gateway_in_process() -> None:
    engine = RecordingEngine()
    session = uuid4()

    reply = GatewayClient(gateway(engine), TOKEN).send("Olá, NeoSon", session)

    assert (reply.text, reply.status) == ("Olá, NeoSon", "completed")
    assert engine.received == [
        UserMessage(text="Olá, NeoSon", session_id=session, principal="http-owner")
    ]


def test_client_raises_gateway_error_with_the_error_contract() -> None:
    with pytest.raises(GatewayError) as caught:
        GatewayClient(gateway(BrokenEngine()), TOKEN).send("oi")

    assert (caught.value.status_code, caught.value.code) == (500, "internal_error")
    assert str(caught.value) == "Erro interno."


def test_client_handles_error_bodies_outside_the_contract() -> None:
    http = mock_http(
        httpx2.MockTransport(lambda request: httpx2.Response(502, text="<html>"))
    )

    with pytest.raises(GatewayError) as caught:
        GatewayClient(http).send("oi")

    assert caught.value.status_code == 502
    assert caught.value.code is None


@pytest.mark.parametrize("body", [b"<html>conteudo-privado</html>", b"{}"])
def test_client_rejects_invalid_success_responses(body: bytes) -> None:
    with mock_http(
        httpx2.MockTransport(lambda request: httpx2.Response(200, content=body))
    ) as http:
        with pytest.raises(GatewayError) as caught:
            GatewayClient(http).send("oi")

    assert caught.value.status_code == 200
    assert caught.value.code is None
    assert str(caught.value) == "O gateway respondeu 200 fora do formato esperado."
    assert caught.value.__suppress_context__


def test_client_reports_an_unreachable_gateway() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("recusada", request=request)

    with pytest.raises(GatewayError) as caught:
        GatewayClient(mock_http(httpx2.MockTransport(refuse))).send("oi")

    assert "Não foi possível falar com o gateway" in str(caught.value)
    assert caught.value.status_code is None


def test_client_sends_the_v1_contract_payload() -> None:
    sent: list[bytes] = []

    def capture(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.content)
        return httpx2.Response(
            200,
            json={"run_id": str(uuid4()), "text": "ok", "status": "completed"},
        )

    GatewayClient(mock_http(httpx2.MockTransport(capture))).send("oi")

    assert sent == [b'{"protocol_version":1,"session_id":null,"text":"oi"}']


# --- configuração ---


def test_gateway_url_precedence_and_default() -> None:
    env = {"NEOSON_GATEWAY_URL": "http://neoson-servidor.local:8000/"}

    assert gateway_url(environ={}) == DEFAULT_GATEWAY_URL
    assert gateway_url(environ=env) == "http://neoson-servidor.local:8000"
    assert gateway_url("https://outro:9000", environ=env) == "https://outro:9000"


@pytest.mark.parametrize(
    "value",
    [
        "neoson-servidor.local:8000",
        "ftp://x",
        "http://",
        "http://gateway.test:abc",
        "http://gateway.test:65536",
        "http://gateway.test:0",
        "http://:8000",
        "http://[invalid",
    ],
)
def test_gateway_url_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="NEOSON_GATEWAY_URL"):
        gateway_url(value, environ={})


@pytest.mark.parametrize("value", ["http://gateway.test:8000/", "https://[::1]:8443/"])
def test_gateway_url_accepts_valid_host_and_port(value: str) -> None:
    assert gateway_url(value, environ={}) == value.rstrip("/")


# --- linha de comando ---


def test_main_prints_the_reply(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["Olá, NeoSon"], http=gateway())

    assert code == 0
    assert capsys.readouterr().out == "Olá, NeoSon\n"


def test_main_returns_1_when_the_gateway_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["oi"], http=gateway(BrokenEngine()))

    assert code == 1
    assert capsys.readouterr().err == "Erro: Erro interno.\n"


@pytest.mark.parametrize("body", [b"<html>conteudo-privado</html>", b"{}"])
def test_main_reports_invalid_success_as_gateway_failure(
    capsys: pytest.CaptureFixture[str], body: bytes
) -> None:
    with mock_http(
        httpx2.MockTransport(lambda request: httpx2.Response(200, content=body))
    ) as http:
        code = main(["oi"], http=http)

    assert code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Erro: O gateway respondeu 200 fora do formato esperado.\n"


def test_main_rejects_an_empty_message_before_sending(
    capsys: pytest.CaptureFixture[str],
) -> None:
    http = mock_http(httpx2.MockTransport(lambda request: pytest.fail("enviou")))

    code = main([""], http=http)

    assert code == 2
    assert "Mensagem inválida" in capsys.readouterr().err


@pytest.mark.parametrize(
    "value",
    ["sem-esquema", "http://gateway.test:abc", "http://:8000", "http://[invalid"],
)
def test_main_rejects_an_invalid_gateway_url(
    capsys: pytest.CaptureFixture[str], value: str
) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["oi", "--gateway-url", value])

    assert caught.value.code == 2
    error = capsys.readouterr().err
    assert "NEOSON_GATEWAY_URL" in error
    assert value not in error


def test_main_handles_url_rejected_by_http_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def invalid_client(*args: object, **kwargs: object) -> httpx2.Client:
        raise httpx2.InvalidURL("detalhe-privado")

    monkeypatch.setattr(httpx2, "Client", invalid_client)
    with pytest.raises(SystemExit) as caught:
        main(["oi", "--gateway-url", "http://gateway.test"])

    assert caught.value.code == 2
    error = capsys.readouterr().err
    assert "NEOSON_GATEWAY_URL" in error
    assert "detalhe-privado" not in error


def test_client_sends_the_api_token() -> None:
    seen: list[str | None] = []

    def capture(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("authorization"))
        return httpx2.Response(
            200, json={"run_id": str(uuid4()), "text": "ok", "status": "completed"}
        )

    GatewayClient(mock_http(httpx2.MockTransport(capture)), "abc").send("oi")

    assert seen == ["Bearer abc"]


def test_main_without_token_is_rejected_by_the_gateway(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("NEOSON_API_TOKEN")

    code = main(["oi"], http=gateway())

    assert code == 1
    assert capsys.readouterr().err == "Erro: Token de API ausente ou inválido.\n"


def test_main_can_show_the_session_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = uuid4()

    def answer(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "run_id": str(uuid4()),
                "text": "ok",
                "status": "completed",
                "session_id": str(session),
            },
        )

    code = main(["oi", "--show-session"], http=mock_http(httpx2.MockTransport(answer)))

    output = capsys.readouterr()
    assert code == 0
    assert (output.out, output.err) == ("ok\n", f"sessão: {session}\n")
