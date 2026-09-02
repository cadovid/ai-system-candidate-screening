"""Provider-independent protocol for language interpretation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic_ai.messages import ModelMessage

from candidate_screening.domain.enums import Language, ScreeningField
from candidate_screening.domain.models import ScreeningDecision, ScreeningState

from .schemas import RecruiterSummaryOutput, TurnInterpretation


@dataclass(slots=True)
class InterpreterDependencies:
    """Trusted server-side context supplied to a Pydantic AI agent."""

    state: ScreeningState
    pending_field: ScreeningField | None = None
    language: Language = Language.ES
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    local_date: str | None = None
    correlation_id: str | None = None


@dataclass(slots=True)
class InterpreterResult:
    """Typed result plus operational metadata; no status is included."""

    interpretation: TurnInterpretation
    new_messages: Sequence[ModelMessage] = ()
    usage: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())
    model_name: str | None = None


class LanguageInterpreter(Protocol):
    async def interpret(
        self,
        message: str,
        dependencies: InterpreterDependencies,
        *,
        message_history: Sequence[ModelMessage] = (),
    ) -> InterpreterResult: ...


class ModelFactory(Protocol):
    """Provider-neutral factory used to keep model construction injectable."""

    def create(self, settings: Any) -> Any: ...


class SummaryGeneratorProtocol(Protocol):
    async def generate(
        self,
        state: ScreeningState,
        decision: ScreeningDecision,
        *,
        language: Language,
    ) -> RecruiterSummaryOutput: ...
