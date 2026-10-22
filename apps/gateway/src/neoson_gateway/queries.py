"""Consultas autenticadas: execução, sessão e resumo do dia (§12)."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from pathlib import Path
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from neoson_contracts.api.v1.messages import MessageStatus
from neoson_contracts.api.v1.queries import (
    MetricsSummary,
    RunResponse,
    RunUsage,
    SessionMessage,
    SessionResponse,
)
from neoson_core.memory.queries import NotFound, run_view, session_page, summary


@dataclass(frozen=True, slots=True)
class QueryConfig:
    db: Path
    zone: tzinfo


def queries_router(
    config: QueryConfig, auth: Callable[..., Awaitable[None]]
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["queries"], dependencies=[Depends(auth)])

    @router.get("/runs/{run_id}", response_model=RunResponse)
    async def get_run(run_id: UUID, response: Response) -> RunResponse:
        response.headers["Cache-Control"] = "no-store"
        try:
            view = await asyncio.to_thread(run_view, config.db, run_id)
        except NotFound:
            raise HTTPException(404, "Execução não encontrada.") from None
        return RunResponse(
            run_id=view.run_id,
            session_id=view.session_id,
            status=cast(MessageStatus, view.status),
            text=view.text,
            model=view.model,
            started_at=view.started_at,
            ended_at=view.ended_at,
            usage=RunUsage(
                input_tokens=view.input_tokens,
                output_tokens=view.output_tokens,
                cached_tokens=view.cached_tokens,
            ),
        )

    @router.get("/sessions/{session_id}", response_model=SessionResponse)
    async def get_session(
        session_id: UUID,
        response: Response,
        before: int | None = Query(default=None, ge=1),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> SessionResponse:
        response.headers["Cache-Control"] = "no-store"
        try:
            page = await asyncio.to_thread(
                session_page, config.db, session_id, before=before, limit=limit
            )
        except NotFound:
            raise HTTPException(404, "Sessão não encontrada.") from None
        return SessionResponse(
            session_id=page.session_id,
            channel=page.channel,
            created_at=page.created_at,
            messages=[
                SessionMessage(
                    id=m.id,
                    role="user" if m.role == "user" else "assistant",
                    content=m.content,
                    created_at=m.created_at,
                    run_id=m.run_id,
                )
                for m in page.messages
            ],
            next_before=page.next_before,
        )

    @router.get("/metrics/summary", response_model=MetricsSummary)
    async def metrics_summary(day: date | None = None) -> MetricsSummary:
        target = day or datetime.now(config.zone).date()
        result = await asyncio.to_thread(summary, config.db, target, config.zone)
        m = result.metrics
        return MetricsSummary(
            day=m.day,
            runs_total=m.runs_total,
            completed=m.completed,
            failed=m.failed,
            waiting_approval=m.waiting_approval,
            input_tokens=m.input_tokens,
            output_tokens=m.output_tokens,
            cached_tokens=m.cached_tokens,
            cost_usd=m.cost_usd,
            runs_without_cost=m.runs_without_cost,
            outbox_pending=result.outbox_pending,
            outbox_dead_letter=result.outbox_dead_letter,
        )

    return router
