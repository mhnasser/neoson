"""Canal autenticado de revisão: autoridade vem do servidor, nunca do modelo."""

from collections.abc import Awaitable, Callable
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, Response

from neoson_contracts.api.v1.actions import (
    ActionDecision,
    ActionResponse,
    ActionResume,
    ActionState,
)
from neoson_core.engine.actions import (
    ActionService,
    ActionView,
)
from neoson_core.engine.tools import fingerprint

# Instalação pessoal: todo portador do bearer é o mesmo proprietário HTTP.
# Não aceitar principal fornecido em JSON/header/query nem compartilhar o token.
HTTP_OWNER = "http-owner"


def response(view: ActionView, description: str | None = None) -> ActionResponse:
    a = view.action
    return ActionResponse(
        id=a.id,
        run_id=a.run_id,
        session_id=a.session_id,
        tool=a.call.name,
        arguments=dict(a.call.arguments),
        fingerprint=fingerprint(a.call),
        expires_at=a.expires_at,
        state=cast(ActionState, view.state),
        tool_description=description,
    )


def actions_router(
    service: ActionService, auth: Callable[..., Awaitable[None]]
) -> APIRouter:
    def describe(view: ActionView) -> ActionResponse:
        description = None
        if view.action.context == service.context:
            description = next(
                (
                    s.description
                    for s in service.tools.specs()
                    if s.name == view.action.call.name
                ),
                None,
            )
        return response(view, description)

    def no_cache(response: Response) -> None:
        response.headers["Cache-Control"] = "no-store"

    router = APIRouter(
        prefix="/api/v1",
        tags=["actions"],
        dependencies=[Depends(auth), Depends(no_cache)],
    )

    @router.get("/actions", response_model=list[ActionResponse])
    async def open_actions() -> list[ActionResponse]:
        """Pendentes ou aprovadas sem execução, inclusive de pedidos por voz."""
        return [describe(v) for v in service.store.open_for(HTTP_OWNER)]

    @router.get("/runs/{run_id}/actions", response_model=list[ActionResponse])
    async def list_actions(run_id: UUID) -> list[ActionResponse]:
        return [describe(v) for v in service.store.for_run(run_id, HTTP_OWNER)]

    @router.get("/actions/{action_id}", response_model=ActionResponse)
    async def get_action(action_id: UUID) -> ActionResponse:
        return describe(service.store.get(action_id, HTTP_OWNER))

    @router.post("/actions/{action_id}/decision", response_model=ActionResponse)
    async def decide(action_id: UUID, body: ActionDecision) -> ActionResponse:
        return describe(service.store.decide(action_id, HTTP_OWNER, body.approve))

    @router.post("/actions/{action_id}/resume", response_model=ActionResponse)
    async def resume(
        action_id: UUID, body: ActionResume | None = None
    ) -> ActionResponse:
        return describe(await service.resume(action_id, HTTP_OWNER))

    return router
