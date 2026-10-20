import dataclasses
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from neoson_core.engine import (
    Budget,
    InvalidTransition,
    Message,
    Role,
    RouteDecision,
    RunContext,
    RunState,
    ToolCall,
    ToolResult,
    ToolResultStatus,
    allowed_transitions,
    transition,
)

# --- mensagens e ferramentas ---


def test_tool_call_arguments_are_a_read_only_copy() -> None:
    arguments: dict[str, object] = {"dia": "2026-10-21"}
    call = ToolCall(id="call-1", name="calendar_list", arguments=arguments)

    arguments["dia"] = "alterado"

    assert call.arguments == {"dia": "2026-10-21"}
    with pytest.raises(TypeError):
        call.arguments["dia"] = "x"  # type: ignore[index]


@pytest.mark.parametrize(("call_id", "name"), [("", "tool"), ("id", " "), ("id", "")])
def test_tool_call_requires_id_and_name(call_id: str, name: str) -> None:
    with pytest.raises(ValueError):
        ToolCall(id=call_id, name=name)


def test_tool_result_accepts_status_text_and_freezes_data() -> None:
    result = ToolResult(
        call_id="call-1",
        status="requires_approval",  # type: ignore[arg-type]
        summary="precisa de aprovação",
        data={"evento": 1},
    )

    assert result.status is ToolResultStatus.REQUIRES_APPROVAL
    with pytest.raises(TypeError):
        result.data["evento"] = 2  # type: ignore[index]


def test_tool_result_rejects_unknown_status() -> None:
    with pytest.raises(ValueError):
        ToolResult(call_id="c", status="talvez", summary="")  # type: ignore[arg-type]


def test_assistant_message_can_request_tools_without_text() -> None:
    call = ToolCall(id="call-1", name="calendar_list")

    message = Message(role=Role.ASSISTANT, content="", tool_calls=(call,))

    assert message.tool_calls == (call,)


def test_tool_message_answers_a_specific_call() -> None:
    message = Message(role="tool", content="2 eventos", tool_call_id="call-1")  # type: ignore[arg-type]

    assert message.role is Role.TOOL


@pytest.mark.parametrize(
    "kwargs",
    [
        {"role": Role.USER, "content": "oi", "tool_calls": (ToolCall("c", "t"),)},
        {"role": Role.TOOL, "content": "resultado"},
        {"role": Role.USER, "content": "oi", "tool_call_id": "c"},
        {"role": Role.USER, "content": ""},
        {"role": "moderador", "content": "oi"},
    ],
)
def test_invalid_messages_are_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Message(**kwargs)


def test_domain_objects_are_immutable() -> None:
    message = Message(role=Role.USER, content="oi")

    with pytest.raises(dataclasses.FrozenInstanceError):
        message.content = "outro"  # type: ignore[misc]


def test_tool_call_is_hashable_and_compares_arguments() -> None:
    first = ToolCall(id="c", name="t", arguments={"a": 1})

    assert hash(first) == hash(ToolCall(id="c", name="t", arguments={"a": 2}))
    assert first == ToolCall(id="c", name="t", arguments={"a": 1})
    assert first != ToolCall(id="c", name="t", arguments={"a": 2})


# --- roteamento ---


def decision(**overrides: Any) -> RouteDecision:
    values: dict[str, Any] = {
        "intent": "agenda",
        "complexity": "low",
        "execution_mode": "direct",
        "primary_model_profile": "balanced",
        "confidence": 0.9,
        "policy_version": "rules-v1",
        "rationale_code": "keyword:agenda",
    }
    return RouteDecision(**(values | overrides))


def test_route_decision_follows_the_router_contract() -> None:
    assert decision().execution_mode == "direct"
    assert decision(confidence=0).confidence == 0
    assert decision(confidence=1).confidence == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"complexity": "extreme"},
        {"execution_mode": "guess"},
        {"confidence": 1.01},
        {"confidence": -0.1},
        {"confidence": float("nan")},
        {"intent": ""},
        {"rationale_code": " "},
        {"policy_version": ""},
    ],
)
def test_route_decision_rejects_invalid_values(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        decision(**overrides)


# --- orçamento e contexto da execução ---


def test_budget_limits_are_validated() -> None:
    assert Budget(max_steps=12, timeout_seconds=60).max_tokens is None

    for invalid in (
        {"max_steps": 0, "timeout_seconds": 60},
        {"max_steps": True, "timeout_seconds": 60},
        {"max_steps": 1, "timeout_seconds": 0},
        {"max_steps": 1, "timeout_seconds": float("inf")},
        {"max_steps": 1, "timeout_seconds": 60, "max_tokens": 0},
        {"max_steps": 1, "timeout_seconds": 60, "max_cost_usd": -1},
    ):
        with pytest.raises(ValueError):
            Budget(**invalid)


def test_run_context_generates_ids_and_computes_the_deadline() -> None:
    started = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    context = RunContext(
        session_id=uuid4(),
        budget=Budget(max_steps=12, timeout_seconds=30),
        started_at=started,
    )
    other = RunContext(session_id=context.session_id, budget=context.budget)

    assert context.run_id != other.run_id
    assert context.trace_id != other.trace_id
    assert context.deadline == started + timedelta(seconds=30)
    assert not context.expired(now=started + timedelta(seconds=29))
    assert context.expired(now=started + timedelta(seconds=30))


def test_run_context_defaults_to_now_in_utc() -> None:
    before = datetime.now(UTC)

    context = RunContext(session_id=uuid4(), budget=Budget(1, 1))

    assert before <= context.started_at <= datetime.now(UTC)
    assert context.started_at.utcoffset() == timedelta(0)


def test_run_context_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError):
        RunContext(
            session_id=uuid4(),
            budget=Budget(1, 1),
            started_at=datetime(2026, 10, 20, 12, 0),
        )


# --- estados da execução (D05) ---

HAPPY_PATH = [
    RunState.RECEIVED,
    RunState.ROUTING,
    RunState.CONTEXT_READY,
    RunState.RUNNING,
    RunState.WAITING_APPROVAL,
    RunState.RUNNING,
    RunState.COMPLETED,
]


def test_d05_happy_path_with_approval() -> None:
    state = HAPPY_PATH[0]
    for target in HAPPY_PATH[1:]:
        state = transition(state, target)

    assert state is RunState.COMPLETED
    assert state.is_terminal


@pytest.mark.parametrize("state", [s for s in RunState if not s.is_terminal])
def test_any_open_state_can_fail_or_be_cancelled(state: RunState) -> None:
    assert {RunState.FAILED, RunState.CANCELLED} <= allowed_transitions(state)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunState.RECEIVED, RunState.RUNNING),
        (RunState.ROUTING, RunState.COMPLETED),
        (RunState.CONTEXT_READY, RunState.WAITING_APPROVAL),
        (RunState.WAITING_APPROVAL, RunState.COMPLETED),
        (RunState.RUNNING, RunState.ROUTING),
        (RunState.RUNNING, RunState.RUNNING),
    ],
)
def test_transitions_outside_d05_are_rejected(
    current: RunState, target: RunState
) -> None:
    with pytest.raises(InvalidTransition):
        transition(current, target)


@pytest.mark.parametrize(
    "state", [RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED]
)
def test_terminal_states_have_no_exit(state: RunState) -> None:
    assert allowed_transitions(state) == frozenset()
    for target in RunState:
        with pytest.raises(InvalidTransition):
            transition(state, target)


def test_engine_imports_only_the_standard_library() -> None:
    """ADR-001: o `engine` não depende de bibliotecas externas."""
    script = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import neoson_core.engine\n"
        "loaded = {m.split('.')[0] for m in set(sys.modules) - before}\n"
        "print(sorted(loaded - set(sys.stdlib_module_names) - {'neoson_core'}))\n"
    )
    output = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    ).stdout.strip()

    assert output == "[]"
