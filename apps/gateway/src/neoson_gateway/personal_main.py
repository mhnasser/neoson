"""Login local explícito; nenhum código ou token passa pelo gateway/LLM."""

import argparse
import asyncio

from neoson_core.engine.integrations import IntegrationError
from neoson_core.infrastructure.config import ConfigError, load_settings
from neoson_gateway.composition import microsoft_oauth


async def login(*, write: bool = False) -> None:
    settings = load_settings()
    provider = settings.personal_write_provider if write else settings.personal_provider
    if provider != "microsoft":
        variable = (
            "NEOSON_PERSONAL_WRITE_PROVIDER" if write else "NEOSON_PERSONAL_PROVIDER"
        )
        raise ConfigError(f"Selecione {variable}=microsoft antes do login.")
    oauth = microsoft_oauth(settings, write=write)
    if write:
        print(
            "Consentimento de escrita: Calendars.ReadWrite e Mail.ReadWrite; "
            "não inclui Mail.Send."
        )
    device = await oauth.begin_login()
    # Endpoint constante: não imprimir message/verification_uri vindos da rede.
    print("Abra https://microsoft.com/devicelogin no navegador local.")
    print("Código temporário (somente neste terminal):", device.user_code, flush=True)
    await oauth.finish_login(device)
    print("Credenciais salvas localmente. Reinicie o gateway para usar.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Consentimento local de leitura pessoal."
    )
    parser.add_argument("command", choices=["login", "login-write"])
    args = parser.parse_args()
    try:
        asyncio.run(login(write=args.command == "login-write"))
    except (IntegrationError, TimeoutError):
        raise SystemExit(
            "Login não concluído. Confira configuração e consentimento."
        ) from None
    except ConfigError as error:
        raise SystemExit(str(error)) from None
    except KeyboardInterrupt:
        raise SystemExit(
            "Login cancelado; nenhuma operação externa foi repetida."
        ) from None


if __name__ == "__main__":
    main()
