"""Formato de erro comum às respostas não 2xx da API v1."""

from typing import Literal

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    location: str
    message: str


class ErrorResponse(BaseModel):
    protocol_version: Literal[1] = 1
    code: str
    message: str
    details: list[ErrorDetail] = Field(default_factory=list)
