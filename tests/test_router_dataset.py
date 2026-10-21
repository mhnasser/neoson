"""Validação do corpus público e seus limites, sem chamar provedores."""

import asyncio
import json
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from neoson_core.engine import RouteRequest
from neoson_core.routing.dataset import (
    MAX_BYTES,
    Category,
    DatasetInvalid,
    RoutingDataset,
    load_dataset,
)
from neoson_core.routing.rules import RulesRouter

SEED = Path(__file__).resolve().parents[1] / "datasets/router/seed-v1.json"


@pytest.fixture
def payload() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(SEED.read_text(encoding="utf-8"))
    return data


def write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_seed_covers_each_category_in_each_split() -> None:
    data = load_dataset(SEED)
    assert data.id == "router_seed"
    assert data.version == "1.0.0"
    assert len(data.cases) == 24
    for split in ("train", "validation", "test"):
        cases = data.select(split)
        assert len(cases) == 8
        assert {c.category for c in cases} == set(get_args(Category))
        assert all(c.outcome.contains for c in cases)
    assert len(data.checksum) == 64
    with pytest.raises(ValidationError):
        data.cases[0].text = "modificado"  # type: ignore[misc]
    with pytest.raises(ValueError, match="Split inválido"):
        data.select("invalid")  # type: ignore[arg-type]


def test_checksum_is_canonical_and_changes_with_expectations(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    original = load_dataset(SEED)
    payload["cases"].reverse()
    reordered = {key: payload[key] for key in reversed(payload)}
    path = write(tmp_path, reordered)
    assert load_dataset(path).checksum == original.checksum
    path.write_text(
        json.dumps(reordered, indent=4, ensure_ascii=True), encoding="utf-8"
    )
    assert load_dataset(path).checksum == original.checksum
    payload["cases"][0]["route"]["complexity"] = "high"
    assert load_dataset(write(tmp_path, payload)).checksum != original.checksum


@pytest.mark.parametrize("field", ["id", "version", "schema_version", "cases"])
def test_missing_required_fields_rejected(
    tmp_path: Path, payload: dict[str, Any], field: str
) -> None:
    del payload[field]
    with pytest.raises(DatasetInvalid):
        load_dataset(write(tmp_path, payload))


@pytest.mark.parametrize("version", ["v1", "1.0", "01.0.0", "1.0.0-beta", "1١.0.0"])
def test_version_is_strict_semantic_release(
    tmp_path: Path, payload: dict[str, Any], version: str
) -> None:
    payload["version"] = version
    with pytest.raises(DatasetInvalid):
        load_dataset(write(tmp_path, payload))


@pytest.mark.parametrize("value", [True, "1", 1.0, 2, None])
def test_schema_version_requires_supported_integer(
    tmp_path: Path, payload: dict[str, Any], value: object
) -> None:
    payload["schema_version"] = value
    with pytest.raises(DatasetInvalid):
        load_dataset(write(tmp_path, payload))


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "../../escape"),
        ("id", "UPPER"),
        ("split", "dev"),
        ("category", "private"),
        ("text", " \t\n"),
        ("text", "a" * 8001),
        ("text", 42),
        ("session_model_profile", "../model"),
    ],
)
def test_invalid_cases_rejected(
    tmp_path: Path, payload: dict[str, Any], field: str, value: object
) -> None:
    payload["cases"][0][field] = value
    with pytest.raises(DatasetInvalid):
        load_dataset(write(tmp_path, payload))


@pytest.mark.parametrize(
    "field,value",
    [
        ("complexity", "extreme"),
        ("execution_mode", "execute_shell"),
        ("intent", ""),
        ("confidence", 0.5),
    ],
)
def test_route_labels_respect_contract(
    tmp_path: Path, payload: dict[str, Any], field: str, value: object
) -> None:
    payload["cases"][0]["route"][field] = value
    with pytest.raises(DatasetInvalid):
        load_dataset(write(tmp_path, payload))


@pytest.mark.parametrize("location", ["root", "case", "outcome"])
def test_unknown_fields_not_silently_dropped(
    tmp_path: Path, payload: dict[str, Any], location: str
) -> None:
    target = payload if location == "root" else payload["cases"][0]
    if location == "outcome":
        target = target["outcome"]
    target["untrusted_extra"] = "sentinela-privada"
    with pytest.raises(DatasetInvalid) as error:
        load_dataset(write(tmp_path, payload))
    assert str(error.value) == "Dataset de roteamento inválido ou ilegível."
    assert "sentinela" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("same_split", [True, False])
def test_duplicate_ids_rejected_even_with_different_inputs(
    tmp_path: Path, payload: dict[str, Any], same_split: bool
) -> None:
    target = 1 if same_split else 8
    payload["cases"][target]["id"] = payload["cases"][0]["id"]
    with pytest.raises(DatasetInvalid):
        load_dataset(write(tmp_path, payload))


@pytest.mark.parametrize(
    "text",
    [
        "Olá, explique o que é um assistente pessoal.",
        "  OLÁ,\nexplique o que é um assistente pessoal.  ",
        "Ola\u0301, explique o que é um assistente pessoal.",
    ],
)
def test_cross_split_leakage_rejected_after_unicode_normalization(
    tmp_path: Path, payload: dict[str, Any], text: str
) -> None:
    payload["cases"][8]["text"] = text
    payload["cases"][8]["session_model_profile"] = "different"
    with pytest.raises(DatasetInvalid):
        load_dataset(write(tmp_path, payload))


@pytest.mark.parametrize(
    "changes",
    [
        {"contains": []},
        {"contains": ["   "]},
        {"contains": ["um", "UM"]},
        {"contains": ["Sim, feito"], "not_contains": ["feito"]},
        {"required_tools": ["email_send"], "forbidden_tools": ["email_send"]},
        {"required_tools": ["email_send", "email_send"]},
        {"approval": "required"},
        {"state": "waiting_approval"},
        {"state": "running"},
        {"approval": True},
    ],
)
def test_unusable_outcome_expectations_rejected(
    tmp_path: Path, payload: dict[str, Any], changes: dict[str, object]
) -> None:
    payload["cases"][0]["outcome"].update(changes)
    with pytest.raises(DatasetInvalid):
        load_dataset(write(tmp_path, payload))


def test_requires_all_splits_and_bounded_case_count(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    payload["cases"] = payload["cases"][:8]
    with pytest.raises(DatasetInvalid):
        load_dataset(write(tmp_path, payload))
    payload["cases"] *= 33
    with pytest.raises(DatasetInvalid):
        load_dataset(write(tmp_path, payload))


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"cases":[{"id":1,"id":2}]}',
        b"{",
        b"[]",
        b"null",
        b"\xff",
        b'{"x":NaN}',
        b'{"x":Infinity}',
    ],
)
def test_invalid_json_and_duplicate_keys_rejected(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "dataset.json"
    path.write_bytes(raw)
    with pytest.raises(DatasetInvalid):
        load_dataset(path)


def test_missing_oversized_and_deeply_nested_input_sanitized(tmp_path: Path) -> None:
    path = tmp_path / "dataset.json"
    with pytest.raises(DatasetInvalid):
        load_dataset(path)
    path.write_bytes(b" " * (MAX_BYTES + 1))
    with pytest.raises(DatasetInvalid):
        load_dataset(path)
    path.write_text("[" * 1500 + "]" * 1500, encoding="utf-8")
    with pytest.raises(DatasetInvalid):
        load_dataset(path)


def test_dataset_is_independent_of_active_rules() -> None:
    data = load_dataset(SEED)
    case = next(c for c in data.cases if c.category == "memory")
    decision = asyncio.run(RulesRouter("primary").decide(RouteRequest(text=case.text)))
    # Rótulos não são derivados da implementação: expõem limites da baseline atual.
    assert decision.policy_version == "rules-v1"
    assert decision.primary_model_profile == "primary"
    assert decision.execution_mode == "direct"
    assert case.route.execution_mode == "retrieve"
    assert isinstance(data, RoutingDataset)
