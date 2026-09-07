from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import select

from candidate_screening.ai.interpreter import InterpreterDependencies, InterpreterResult
from candidate_screening.ai.schemas import ExtractedValue, TurnInterpretation
from candidate_screening.application.conversation import ConversationController
from candidate_screening.application.coordinator import CoordinatorError, TurnCoordinator
from candidate_screening.config import Settings
from candidate_screening.domain.enums import InteractionMode, Language, ScreeningStatus, TurnStatus
from candidate_screening.domain.rules import ScreeningEngine
from candidate_screening.domain.service_areas import ServiceAreaMatcher
from candidate_screening.main import create_app
from candidate_screening.persistence import Base, SqlAlchemyUnitOfWork
from candidate_screening.persistence.database import (
    create_async_engine_for_url,
    create_session_factory,
)
from candidate_screening.persistence.orm import AuditEventORM, MessageORM, TurnORM

DATA_ROOT = Path(__file__).parents[1] / "data"


class _QueueInterpreter:
    """Deterministic interpreter used to prove mode-independent orchestration."""

    def __init__(self, outputs: Sequence[TurnInterpretation]) -> None:
        self.outputs = tuple(outputs)
        self.calls = 0

    async def interpret(
        self,
        message: str,
        dependencies: InterpreterDependencies,
        *,
        message_history: Sequence[Any] = (),
    ) -> InterpreterResult:
        _ = (message, dependencies, message_history)
        index = min(self.calls, len(self.outputs) - 1)
        self.calls += 1
        return InterpreterResult(interpretation=self.outputs[index])


class _FailingInterpreter:
    async def interpret(
        self,
        message: str,
        dependencies: InterpreterDependencies,
        *,
        message_history: Sequence[Any] = (),
    ) -> InterpreterResult:
        _ = (message, dependencies, message_history)
        raise RuntimeError("provider unavailable")


def _build_coordinator(factory: Any, interpreter: Any) -> TurnCoordinator:
    matcher = ServiceAreaMatcher.from_file(DATA_ROOT / "service_areas" / "service_areas.json")
    controller = ConversationController(ScreeningEngine(), matcher)
    return TurnCoordinator(
        lambda: SqlAlchemyUnitOfWork(factory),
        controller,
        interpreter,
    )


async def _database(tmp_path: Path) -> tuple[Any, Any]:
    engine = create_async_engine_for_url(f"sqlite:///{tmp_path / 'voice.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, create_session_factory(engine)


def _settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        service_areas_path=DATA_ROOT / "service_areas" / "service_areas.json",
        faq_path=DATA_ROOT / "faq" / "faq.json",
    )


def _stable_state_dump(state: object) -> object:
    """Remove audit timestamps/turn IDs before comparing isolated sessions."""

    if isinstance(state, Mapping):
        mapping = cast(Mapping[object, object], state)
        return {
            key: _stable_state_dump(value)
            for key, value in mapping.items()
            if key not in {"captured_at", "message_id"}
        }
    if isinstance(state, list):
        return [_stable_state_dump(value) for value in cast(list[object], state)]
    return state


@pytest.mark.asyncio
async def test_candidate_api_defaults_to_text_and_persists_voice_mode(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    interpreter = _QueueInterpreter(
        [
            TurnInterpretation(
                full_name=ExtractedValue(value="Ana García", provided=True),
            ),
            TurnInterpretation(
                drivers_license=ExtractedValue(value=True, provided=True),
            ),
        ]
    )
    coordinator = _build_coordinator(factory, interpreter)
    app = create_app(
        settings=_settings(f"sqlite:///{tmp_path / 'voice.db'}"), coordinator=coordinator
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/api/v1/candidate/conversations", json={"language": "en", "channel": "web"}
        )
        assert created.status_code == 201
        payload = created.json()
        authorization = {"Authorization": f"Bearer {payload['resume_token']}"}

        text_turn = await client.post(
            f"/api/v1/candidate/conversations/{payload['conversation_id']}/turns",
            headers={**authorization, "Idempotency-Key": "text-turn"},
            json={"message": "Ana García"},
        )
        assert text_turn.status_code == 200

        voice_turn = await client.post(
            f"/api/v1/candidate/conversations/{payload['conversation_id']}/turns",
            headers={**authorization, "Idempotency-Key": "voice-turn"},
            json={"message": "Sí, tengo licencia", "input_mode": "voice"},
        )
        assert voice_turn.status_code == 200

    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert uow.session is not None
        turns = tuple(
            (
                await uow.session.scalars(select(TurnORM).order_by(TurnORM.created_at, TurnORM.id))
            ).all()
        )
        assert [turn.input_mode for turn in turns] == ["text", "voice"]
        assert all(turn.status == TurnStatus.COMPLETED.value for turn in turns)

        events = tuple(
            (
                await uow.session.scalars(
                    select(AuditEventORM)
                    .where(AuditEventORM.turn_id.in_([turn.id for turn in turns]))
                    .order_by(AuditEventORM.created_at, AuditEventORM.id)
                )
            ).all()
        )
        started = [event for event in events if event.event_type == "turn_started"]
        completed = [event for event in events if event.event_type == "turn_completed"]
        assert [event.event_metadata["input_mode"] for event in started] == ["text", "voice"]
        assert [event.event_metadata["input_mode"] for event in completed] == ["text", "voice"]

        messages = tuple((await uow.session.scalars(select(MessageORM))).all())
        user_messages = [message for message in messages if message.direction == "user"]
        assert [message.content for message in user_messages] == [
            "Ana García",
            "Sí, tengo licencia",
        ]
        assert all(isinstance(message.content, str) for message in messages)

    await engine.dispose()


@pytest.mark.asyncio
async def test_voice_and_text_turns_have_identical_canonical_screening_results(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    outputs = [
        TurnInterpretation(
            detected_language=Language.EN,
            full_name=ExtractedValue(value="Alex Example", provided=True),
        ),
        TurnInterpretation(
            detected_language=Language.EN,
            drivers_license=ExtractedValue(value=False, provided=True),
        ),
        TurnInterpretation(
            detected_language=Language.EN,
            confirmation=True,
        ),
    ]
    text_coordinator = _build_coordinator(factory, _QueueInterpreter(outputs))
    voice_coordinator = _build_coordinator(factory, _QueueInterpreter(outputs))
    text_conversation = await text_coordinator.create_conversation(language=Language.EN)
    voice_conversation = await voice_coordinator.create_conversation(language=Language.EN)

    text_first = await text_coordinator.process_turn(
        text_conversation.conversation_id,
        "alex example",
        "text-name",
        input_mode=InteractionMode.TEXT,
    )
    text_terminal = await text_coordinator.process_turn(
        text_conversation.conversation_id,
        "No license",
        "text-license",
        input_mode=InteractionMode.TEXT,
    )
    text_confirmation = await text_coordinator.process_turn(
        text_conversation.conversation_id,
        "Yes",
        "text-license-confirm",
        input_mode=InteractionMode.TEXT,
    )
    voice_first = await voice_coordinator.process_turn(
        voice_conversation.conversation_id,
        "alex example",
        "voice-name",
        input_mode=InteractionMode.VOICE,
    )
    voice_terminal = await voice_coordinator.process_turn(
        voice_conversation.conversation_id,
        "No license",
        "voice-license",
        input_mode=InteractionMode.VOICE,
    )
    voice_confirmation = await voice_coordinator.process_turn(
        voice_conversation.conversation_id,
        "Yes",
        "voice-license-confirm",
        input_mode=InteractionMode.VOICE,
    )

    assert (
        text_first.screening_status is voice_first.screening_status is ScreeningStatus.IN_PROGRESS
    )
    assert (
        text_terminal.screening_status
        is voice_terminal.screening_status
        is ScreeningStatus.IN_PROGRESS
    )
    assert (
        text_confirmation.screening_status
        is voice_confirmation.screening_status
        is ScreeningStatus.DISQUALIFIED
    )
    assert text_confirmation.decision is not None and voice_confirmation.decision is not None
    text_decision = text_confirmation.decision.model_dump(mode="json")
    voice_decision = voice_confirmation.decision.model_dump(mode="json")
    # Decision timestamps describe when each independent test conversation was
    # evaluated; all reproducible decision content must otherwise match.
    text_decision.pop("decided_at", None)
    voice_decision.pop("decided_at", None)
    assert text_decision == voice_decision

    text_view = await text_coordinator.get_conversation(text_conversation.conversation_id)
    voice_view = await voice_coordinator.get_conversation(voice_conversation.conversation_id)
    assert _stable_state_dump(text_view.state.model_dump(mode="json")) == _stable_state_dump(
        voice_view.state.model_dump(mode="json")
    )
    assert text_view.status is voice_view.status

    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert uow.session is not None
        rows = tuple((await uow.session.scalars(select(TurnORM))).all())
        assert sorted(turn.input_mode for turn in rows) == [
            "text",
            "text",
            "text",
            "voice",
            "voice",
            "voice",
        ]
        assert set(Base.metadata.tables).isdisjoint({"audio", "voice_recordings"})

    await engine.dispose()


@pytest.mark.asyncio
async def test_voice_provider_failure_is_replayable_and_audited_with_mode(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    coordinator = _build_coordinator(factory, _FailingInterpreter())
    created = await coordinator.create_conversation(language=Language.EN)

    failed = await coordinator.process_turn(
        created.conversation_id,
        "spoken answer",
        "voice-failure",
        input_mode="voice",
    )
    replayed = await coordinator.process_turn(
        created.conversation_id,
        "spoken answer",
        "voice-failure",
        input_mode=InteractionMode.VOICE,
    )

    assert failed.status is TurnStatus.FAILED
    assert failed.error_code == "provider_unavailable"
    assert replayed.idempotent is True
    assert replayed.status is TurnStatus.FAILED

    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert uow.session is not None
        turn = await uow.session.scalar(
            select(TurnORM).where(TurnORM.idempotency_key == "voice-failure")
        )
        assert turn is not None and turn.input_mode == "voice"
        event = await uow.session.scalar(
            select(AuditEventORM).where(
                AuditEventORM.turn_id == turn.id,
                AuditEventORM.event_type == "turn_failed",
            )
        )
        assert event is not None
        assert event.event_metadata == {"error_code": "provider_unavailable", "input_mode": "voice"}

    await engine.dispose()


@pytest.mark.asyncio
async def test_direct_coordinator_rejects_unknown_input_mode_without_writing(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    coordinator = _build_coordinator(factory, _QueueInterpreter([TurnInterpretation()]))
    created = await coordinator.create_conversation(language=Language.EN)

    with pytest.raises(CoordinatorError) as raised:
        await coordinator.process_turn(
            created.conversation_id,
            "answer",
            "invalid-mode",
            input_mode="audio",
        )
    assert raised.value.code == "invalid_input_mode"

    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert uow.session is not None
        assert (
            await uow.session.scalar(
                select(TurnORM).where(TurnORM.idempotency_key == "invalid-mode")
            )
            is None
        )

    await engine.dispose()
