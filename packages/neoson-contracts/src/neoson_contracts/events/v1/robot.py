from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

from neoson_contracts.api.v1.errors import ErrorResponse
from neoson_contracts.api.v1.voice import VoiceResponse


class RobotState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    USING_TOOL = "using_tool"
    SPEAKING = "speaking"
    ERROR = "error"
    OFFLINE = "offline"


class VoiceProgress(BaseModel):
    protocol_version: Literal[1] = 1
    type: Literal["progress"] = "progress"
    state: Literal["transcribing", "thinking", "using_tool"]


# Expressão do rosto pedida por um emoji da resposta (o emoji não é falado).
Expression = Literal["feliz", "surpreso", "triste", "carinho", "pensando"]


class VoiceSpeech(BaseModel):
    """Uma frase da resposta já sintetizada (WAV 16 kHz), na ordem de `index`."""

    protocol_version: Literal[1] = 1
    type: Literal["speech"] = "speech"
    index: int = Field(ge=0, le=511)
    audio_base64: str
    # Opcional: cliente antigo ignora, gateway antigo não manda.
    expression: Expression | None = None


class VoiceCompleted(BaseModel):
    protocol_version: Literal[1] = 1
    type: Literal["result"] = "result"
    result: VoiceResponse


class VoiceFailed(BaseModel):
    protocol_version: Literal[1] = 1
    type: Literal["error"] = "error"
    status_code: int = Field(ge=400, le=599)
    error: ErrorResponse


VoiceEvent = Annotated[
    VoiceProgress | VoiceSpeech | VoiceCompleted | VoiceFailed,
    Field(discriminator="type"),
]
VOICE_EVENT = TypeAdapter(VoiceEvent)
