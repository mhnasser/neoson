"""Mensagens do usuário e respostas do NeoSon: `POST /api/v1/messages`."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

MessageStatus = Literal["completed", "needs_approval", "failed"]


class MessageRequest(BaseModel):
    protocol_version: Literal[1] = 1
    session_id: UUID | None = None
    text: str = Field(min_length=1, max_length=8000)


class MessageResponse(BaseModel):
    protocol_version: Literal[1] = 1
    run_id: UUID
    text: str
    status: MessageStatus
