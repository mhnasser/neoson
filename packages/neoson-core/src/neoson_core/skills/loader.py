"""Carrega e valida uma skill do disco (§7.1, ADR-007).

Uma skill é uma pasta `skill_library/<id>/` com `SKILL.md`, `metadata.yaml` e,
opcionalmente, `examples/` e `tests/fixtures/`. Só arquivos de texto: scripts de
skill ainda não são aceitos. O checksum cobre todos os arquivos, com quebras de
linha normalizadas, para dar o mesmo valor no Windows e no Linux.
"""

import hashlib
import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from neoson_core.engine.skills import Skill, SkillRef, SkillRisk

SKILL_FILE = "SKILL.md"
METADATA_FILE = "metadata.yaml"
OPTIONAL_DIRS = ("examples", "tests/fixtures")
TEXT_SUFFIXES = frozenset({".md", ".yaml", ".yml", ".json", ".txt"})
REQUIRED_SECTIONS = ("Quando usar", "Passos", "Restrições", "Saída")
MAX_INSTRUCTIONS = 8000

_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


class SkillMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=10, max_length=300)
    triggers: tuple[str, ...] = Field(min_length=1, max_length=20)
    allowed_tools: tuple[str, ...] = ()
    requires_approval: bool = False
    risk_level: Literal["low", "medium", "high"]
    owner: str = Field(min_length=1, max_length=60)

    @field_validator("triggers")
    @classmethod
    def _triggers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(" ".join(t.split()) for t in value)
        if any(not t or len(t) > 60 for t in cleaned):
            raise ValueError("cada gatilho precisa ter de 1 a 60 caracteres")
        return cleaned

    @field_validator("allowed_tools")
    @classmethod
    def _unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("ferramentas repetidas")
        return value


class SkillInvalid(Exception):
    """A pasta não é uma skill válida; `problems` lista todos os motivos."""

    def __init__(self, path: Path, problems: Iterable[str]) -> None:
        self.path = path
        self.problems = tuple(problems)
        super().__init__(f"{path}: " + "; ".join(self.problems))


@dataclass(frozen=True, slots=True)
class SkillPackage:
    """Uma skill lida do disco e validada (ainda não aprovada)."""

    path: Path
    metadata: SkillMetadata
    instructions: str
    checksum: str
    files: tuple[str, ...]

    def skill(self) -> Skill:
        meta = self.metadata
        return Skill(
            ref=SkillRef(
                id=meta.id,
                version=meta.version,
                description=meta.description,
                triggers=meta.triggers,
            ),
            instructions=self.instructions,
            allowed_tools=frozenset(meta.allowed_tools),
            requires_approval=meta.requires_approval,
            risk_level=SkillRisk(meta.risk_level),
            checksum=self.checksum,
        )


def load_skill(path: Path, known_tools: Collection[str] | None = None) -> SkillPackage:
    """Lê e valida a pasta; `SkillInvalid` com todos os problemas encontrados.

    Com `known_tools`, as `allowed_tools` precisam existir entre elas.
    """
    if not path.is_dir():
        raise SkillInvalid(path, ["pasta não encontrada"])
    files = _files(path)
    problems = _layout_problems(path, files)
    texts: dict[str, str] = {}
    for name in files:
        try:
            texts[name] = _read_text(path / name)
        except UnicodeDecodeError:
            problems.append(f"{name}: não está em UTF-8")

    metadata = None
    if METADATA_FILE in texts:
        metadata, metadata_problems = _metadata(texts[METADATA_FILE])
        problems.extend(metadata_problems)
    if metadata is not None:
        if metadata.id != path.name:
            problems.append(
                f"id '{metadata.id}' diferente do nome da pasta '{path.name}'"
            )
        if known_tools is not None:
            unknown = sorted(set(metadata.allowed_tools) - set(known_tools))
            if unknown:
                problems.append("ferramentas desconhecidas: " + ", ".join(unknown))

    instructions = texts.get(SKILL_FILE, "").strip()
    if SKILL_FILE in texts:
        problems.extend(_instruction_problems(instructions))

    if problems or metadata is None:
        raise SkillInvalid(path, problems)
    return SkillPackage(
        path=path,
        metadata=metadata,
        instructions=instructions,
        checksum=checksum(texts),
        files=tuple(files),
    )


def load_library(
    root: Path, known_tools: Collection[str] | None = None
) -> tuple[list[SkillPackage], list[SkillInvalid]]:
    """Todas as skills em `root` (uma por subpasta): as válidas e os erros."""
    valid: list[SkillPackage] = []
    errors: list[SkillInvalid] = []
    if not root.is_dir():
        return valid, errors
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        try:
            valid.append(load_skill(folder, known_tools))
        except SkillInvalid as error:
            errors.append(error)
    return valid, errors


def checksum(texts: dict[str, str]) -> str:
    """SHA-256 dos caminhos relativos e conteúdos (com quebras de linha em LF)."""
    digest = hashlib.sha256()
    for name in sorted(texts):
        content = hashlib.sha256(texts[name].encode("utf-8")).hexdigest()
        digest.update(f"{name}\0{content}\n".encode())
    return digest.hexdigest()


def _files(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    )


def _layout_problems(root: Path, files: list[str]) -> list[str]:
    problems = [
        f"falta {required}"
        for required in (SKILL_FILE, METADATA_FILE)
        if required not in files
    ]
    for name in files:
        if name in (SKILL_FILE, METADATA_FILE):
            continue
        if Path(name).suffix.lower() not in TEXT_SUFFIXES:
            problems.append(
                f"{name}: tipo de arquivo não permitido (só texto; scripts de skill"
                " ainda não são aceitos)"
            )
        elif not any(name.startswith(prefix + "/") for prefix in OPTIONAL_DIRS):
            problems.append(f"{name}: fora de {', '.join(OPTIONAL_DIRS)}")
    return problems


def _read_text(file: Path) -> str:
    text = file.read_bytes().decode("utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _metadata(text: str) -> tuple[SkillMetadata | None, list[str]]:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        return None, [f"{METADATA_FILE}: YAML inválido ({error.__class__.__name__})"]
    if not isinstance(raw, dict):
        return None, [f"{METADATA_FILE}: precisa ser um mapeamento chave: valor"]
    try:
        return SkillMetadata.model_validate(raw), []
    except ValidationError as error:
        return None, [
            f"{METADATA_FILE}: {'.'.join(str(p) for p in d['loc']) or 'campos'}:"
            f" {d['msg']}"
            for d in error.errors(include_input=False, include_url=False)
        ]


def _instruction_problems(instructions: str) -> list[str]:
    sections = {match.strip() for match in _HEADING.findall(instructions)}
    problems = [
        f"{SKILL_FILE}: falta a seção '## {name}'"
        for name in REQUIRED_SECTIONS
        if name not in sections
    ]
    if len(instructions) > MAX_INSTRUCTIONS:
        problems.append(
            f"{SKILL_FILE}: {len(instructions)} caracteres (máximo {MAX_INSTRUCTIONS})"
        )
    return problems
