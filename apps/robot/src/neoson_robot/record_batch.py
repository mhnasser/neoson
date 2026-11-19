"""Kit de frases fictícias para gravar um corpus local, sem enviar áudio à rede."""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from neoson_robot.pipewire import DeviceError, PipeWireCapture

PHRASES = (
    "Olá, NeoSon.",
    "Qual é a data de hoje?",
    "Que horas são agora?",
    "Quanto é sete vezes oito?",
    "Explique como a chuva se forma.",
    "Responda em uma frase curta.",
    "Meu personagem favorito se chama Aurora.",
    "O personagem Bento mora na cidade fictícia de Vila Azul.",
    "O aniversário de Clara é vinte e três de maio.",
    "O encontro de teste será em dois de dezembro de dois mil e vinte e sete.",
    "Marque um compromisso fictício amanhã às nove horas.",
    "O compromisso termina às dez e meia da manhã.",
    "Quero revisar os detalhes antes de aprovar.",
    "Não aprove essa ação.",
    "Cancele o pedido anterior.",
    "Crie um rascunho fictício sem enviar mensagem.",
    "Leia o título do próximo compromisso de teste.",
    "Procure no histórico a receita de bolo de cenoura.",
    "O projeto fictício se chama Jardim Lunar.",
    "Guarde que o personagem Davi prefere chá de hortelã.",
    "Corrija a preferência do personagem Davi para chá de camomila.",
    "Esqueça a preferência fictícia que acabei de informar.",
    "Quanto é cento e vinte e cinco dividido por cinco?",
    "A temperatura de teste é vinte e dois graus.",
    "O código fictício tem os dígitos quatro, zero, sete e dois.",
    "Liste três cores: azul, verde e amarelo.",
    "Como se escreve a palavra exceção?",
    "Explique a diferença entre memória e armazenamento.",
    "Repita devagar: três pratos de trigo.",
    "Obrigado, NeoSon. Encerramos o teste.",
)


def record_batch(output: Path, capture: PipeWireCapture, *, dry_run: bool) -> None:
    output = output.expanduser().resolve()
    if any((parent / ".git").exists() for parent in (output, *output.parents)):
        raise ValueError("Escolha uma pasta fora de qualquer checkout Git.")
    if dry_run:
        for index, text in enumerate(PHRASES, 1):
            print(f"{index:02d}.wav: {text}")
        return
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    with open(
        output / "manifest.jsonl",
        "x",
        encoding="utf-8",
        opener=lambda path, flags: os.open(path, flags, 0o600),
    ) as manifest:
        for index, text in enumerate(PHRASES, 1):
            input(
                f"[{index}/{len(PHRASES)}] {text}\n"
                "Enter para começar a gravar; Ctrl+C para parar. "
            )
            audio = capture.record(lambda: False)
            name = f"{index:02d}.wav"
            with open(
                output / name,
                "xb",
                opener=lambda path, flags: os.open(path, flags, 0o600),
            ) as file:
                file.write(audio)
            manifest.write(
                json.dumps(
                    {
                        "audio": name,
                        "expected_text": text,
                        "sample_rate": 16000,
                        "channels": 1,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            manifest.flush()
            print("Gravação salva.", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="pasta nova, fora do Git"
    )
    parser.add_argument(
        "--seconds", type=float, default=5, help="tempo por frase, até 15 s"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        record_batch(args.output, PipeWireCapture(args.seconds), dry_run=args.dry_run)
    except (KeyboardInterrupt, EOFError):
        print(
            "Gravação interrompida; amostras concluídas foram preservadas.",
            file=sys.stderr,
        )
        return 130
    except (OSError, ValueError, DeviceError):
        print(
            "Falha ao gravar. Confira PipeWire, duração e pasta nova fora do Git.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
