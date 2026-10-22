"""Captura explícita; display recebe somente estados e avisos fixos."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from threading import Lock
from typing import Protocol
from uuid import UUID

from neoson_contracts.api.v1.voice import VoiceResponse
from neoson_contracts.events.v1.robot import (
    RobotState,
    VoiceCompleted,
    VoiceEvent,
    VoiceFailed,
    VoiceProgress,
)
from neoson_robot.client import GatewayError
from neoson_robot.voice_client import check_audio, response_audio


class Feedback(StrEnum):
    NONE = "none"
    APPROVAL = "approval_required"
    NO_AUDIO = "audio_unavailable"
    CANCELLED = "cancelled"
    ERROR = "error"
    SIMULATED = "simulated"


class Capture(Protocol):
    simulated: bool

    def record(self, cancelled: Callable[[], bool]) -> bytes: ...
    def stop(self) -> None: ...


class Playback(Protocol):
    def play(self, audio: bytes, cancelled: Callable[[], bool]) -> None: ...
    def stop(self) -> None: ...


class Display(Protocol):
    def show(self, state: RobotState, feedback: Feedback = Feedback.NONE) -> None: ...


class VoiceGateway(Protocol):
    def events(
        self, audio: bytes, session_id: UUID | None, simulated_capture: bool
    ) -> Iterator[VoiceEvent]: ...


class SessionState(Protocol):
    def load(self) -> UUID | None: ...
    def save(self, session_id: UUID) -> None: ...


class Cancelled(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Outcome:
    state: RobotState
    feedback: Feedback
    response: VoiceResponse | None = field(default=None, repr=False)


class VoiceController:
    def __init__(
        self,
        capture: Capture,
        playback: Playback,
        display: Display,
        gateway: VoiceGateway,
        sessions: SessionState | None = None,
    ) -> None:
        self.capture, self.playback, self.display, self.gateway = (
            capture,
            playback,
            display,
            gateway,
        )
        self.sessions = sessions
        self.session_id = sessions.load() if sessions is not None else None
        self.state = RobotState.IDLE
        self._lock = Lock()
        self._stream: Iterator[VoiceEvent] | None = None
        self._show(RobotState.IDLE)

    def _show(self, state: RobotState, feedback: Feedback = Feedback.NONE) -> None:
        self.state = state
        self.display.show(state, feedback)

    def interact(
        self, activated: bool, cancelled: Callable[[], bool] | None = None
    ) -> Outcome:
        if not activated:
            return Outcome(self.state, Feedback.NONE)
        if not self._lock.acquire(blocking=False):
            return Outcome(self.state, Feedback.ERROR)
        self._stream = None
        failed = False
        outcome = Outcome(RobotState.ERROR, Feedback.ERROR)
        try:
            outcome = self._interact(cancelled)
        except Exception:
            self.state = RobotState.ERROR
            try:
                self._show(RobotState.ERROR, Feedback.ERROR)
            except Exception:
                pass
        finally:
            try:
                actions = [self.capture.stop, self.playback.stop]
                if self._stream is not None and hasattr(self._stream, "close"):
                    actions.append(self._stream.close)
                for action in actions:
                    try:
                        action()
                    except Exception:
                        failed = True
            finally:
                self._stream = None
                self._lock.release()
        if failed:
            self.state = RobotState.ERROR
            try:
                self._show(RobotState.ERROR, Feedback.ERROR)
            except Exception:
                pass
            return Outcome(RobotState.ERROR, Feedback.ERROR, outcome.response)
        return outcome

    def _interact(self, cancelled: Callable[[], bool] | None) -> Outcome:
        stop = cancelled or (lambda: False)
        result: VoiceResponse | None = None
        try:
            if stop():
                raise Cancelled
            # Falha do indicador impede abrir a captura.
            self._show(RobotState.LISTENING)
            try:
                audio = self.capture.record(stop)
            finally:
                self.capture.stop()
            if stop():
                raise Cancelled
            check_audio(audio)
            self._show(RobotState.THINKING)
            self._stream = self.gateway.events(
                audio, self.session_id, self.capture.simulated
            )
            for event in self._stream:
                if stop():
                    raise Cancelled
                if isinstance(event, VoiceProgress):
                    self._show(RobotState(event.state))
                elif isinstance(event, VoiceFailed):
                    raise GatewayError(
                        "Falha no processamento de voz.", event.status_code
                    )
                elif isinstance(event, VoiceCompleted):
                    if result is not None:
                        raise GatewayError("Resultado de voz duplicado.")
                    result = event.result
            if result is None:
                raise GatewayError("Resultado de voz ausente.")
            if result.session_id is not None:
                self.session_id = result.session_id
                if self.sessions:
                    self.sessions.save(result.session_id)
            if stop():
                raise Cancelled
            media = response_audio(result)
            if media is not None:
                self._show(RobotState.SPEAKING)
                self.playback.play(media, stop)
            if stop():
                raise Cancelled
            feedback = (
                Feedback.APPROVAL
                if result.status == "needs_approval"
                else Feedback.ERROR
                if result.status == "failed"
                else Feedback.NO_AUDIO
                if media is None
                else Feedback.SIMULATED
                if result.simulated
                else Feedback.NONE
            )
            state = RobotState.ERROR if result.status == "failed" else RobotState.IDLE
            self._show(state, feedback)
            return Outcome(state, feedback, result)
        except (Cancelled, KeyboardInterrupt):
            self._show(RobotState.IDLE, Feedback.CANCELLED)
            return Outcome(RobotState.IDLE, Feedback.CANCELLED, result)
        except Exception as error:
            state = (
                RobotState.OFFLINE
                if isinstance(error, GatewayError) and error.code == "offline"
                else RobotState.ERROR
            )
            # Falha de dispositivo/display é ERROR, não evidência de rede offline.
            if not isinstance(error, GatewayError):
                state = RobotState.ERROR
            self.state = state
            try:
                self._show(state, Feedback.ERROR)
            except Exception:
                pass
            return Outcome(state, Feedback.ERROR, result)
