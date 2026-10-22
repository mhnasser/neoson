"""Login local explícito; nenhum código ou token passa pelo gateway/LLM."""

import argparse
import asyncio

from neoson_core.engine.integrations import IntegrationError
from neoson_core.infrastructure.config import ConfigError, load_settings
from neoson_gateway.composition import microsoft_oauth


async def login() -> None:
    settings = load_settings()
    if settings.personal_provider != "microsoft":
        raise ConfigError(
            "Selecione NEOSON_PERSONAL_PROVIDER=microsoft antes do login."
        )
    oauth = microsoft_oauth(settings)
    device = await oauth.begin_login()
    # Endpoint constante: não imprimir message/verification_uri vindos da rede.
    print("Abra https://microsoft.com/devicelogin no navegador local.")
    print("Código temporário (somente neste terminal):", device.user_code, flush=True)
    await oauth.finish_login(device)
    print("Credenciais de leitura salvas localmente. Reinicie o gateway para usar.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Consentimento local de leitura pessoal."
    )
    parser.add_argument("command", choices=["login"])
    parser.parse_args()
    try:
        asyncio.run(login())
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
