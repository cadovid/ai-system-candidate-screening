from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import httpx
import pytest
from pydantic import SecretStr

from candidate_screening.application.coordinator import (
    AnalyticsView,
    ConversationCreated,
    ConversationView,
    ScreeningView,
    TurnCoordinator,
    TurnCoordinatorResult,
)
from candidate_screening.config import Settings
from candidate_screening.domain.enums import (
    ConversationStatus,
    Language,
    ScreeningStatus,
    TurnStatus,
)
from candidate_screening.domain.models import ScreeningState
from candidate_screening.main import create_app


class FakeCoordinator:
    def __init__(self) -> None:
        self.turn_keys: list[str] = []

    async def create_conversation(self, **_: object) -> ConversationCreated:
        return ConversationCreated(
            "conversation-1", "session-1", "resume-1", Language.EN, "Welcome"
        )

    async def verify_resume_token(self, conversation_id: str, token: str) -> bool:
        return conversation_id == "conversation-1" and token == "resume-1"

    async def get_conversation(self, conversation_id: str) -> ConversationView:
        return ConversationView(
            conversation_id=conversation_id,
            session_id="session-1",
            status=ConversationStatus.ACTIVE,
            language=Language.EN,
            state=ScreeningState.empty(Language.EN),
            state_version=1,
            decision=None,
            messages=(
                {
                    "id": "message-1",
                    "direction": "assistant",
                    "content": "Welcome",
                    "language": Language.EN,
                    "created_at": datetime.now(UTC),
                },
            ),
        )

    async def process_turn(
        self,
        conversation_id: str,
        message: str,
        idempotency_key: str,
        *,
        correlation_id: str | None = None,
    ) -> TurnCoordinatorResult:
        _ = (message, correlation_id)
        self.turn_keys.append(idempotency_key)
        return TurnCoordinatorResult(
            conversation_id=conversation_id,
            turn_id="turn-1",
            status=TurnStatus.COMPLETED,
            screening_status=ScreeningStatus.IN_PROGRESS,
            assistant_message="Next question",
            state_version=2,
        )

    async def list_screenings(self, **_: object) -> tuple[ScreeningView, ...]:
        return ()

    async def get_analytics(self) -> AnalyticsView:
        return AnalyticsView(0, {}, 0, 0, 0, None, 0, 0, 0, 0)

    async def retry_summary(self, session_id: str) -> ScreeningView:
        assert session_id == "session-1"
        return ScreeningView(
            session_id=session_id,
            conversation_id="conversation-1",
            candidate_id="candidate-1",
            candidate_name="Candidate",
            status=ScreeningStatus.QUALIFIED,
            state=ScreeningState.empty(Language.EN),
            state_version=2,
            decision=None,
            summary="Repaired summary",
            summary_status="generated",
            handoff_status="ready",
        )


async def _client(coordinator: FakeCoordinator) -> httpx.AsyncClient:
    app = create_app(
        settings=Settings(database_url="sqlite:///unused", internal_api_key=SecretStr("internal")),
        coordinator=cast(TurnCoordinator, coordinator),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_candidate_routes_require_resume_token_and_idempotency() -> None:
    coordinator = FakeCoordinator()
    async with await _client(coordinator) as client:
        created = await client.post("/api/v1/candidate/conversations", json={})
        assert created.status_code == 201
        payload = created.json()
        assert payload["resume_token"] == "resume-1"

        unauthorized = await client.get("/api/v1/candidate/conversations/conversation-1")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["code"] == "missing_bearer_token"

        conversation = await client.get(
            "/api/v1/candidate/conversations/conversation-1",
            headers={"Authorization": "Bearer resume-1"},
        )
        assert conversation.status_code == 200
        assert conversation.json()["messages"][0]["content"] == "Welcome"

        missing_key = await client.post(
            "/api/v1/candidate/conversations/conversation-1/turns",
            headers={"Authorization": "Bearer resume-1"},
            json={"message": "Ana"},
        )
        assert missing_key.status_code == 422
        assert missing_key.json()["error"]["code"] == "missing_idempotency_key"

        turn = await client.post(
            "/api/v1/candidate/conversations/conversation-1/turns",
            headers={"Authorization": "Bearer resume-1", "Idempotency-Key": "turn-key"},
            json={"message": "Ana"},
        )
        assert turn.status_code == 200
        assert coordinator.turn_keys == ["turn-key"]


@pytest.mark.asyncio
async def test_internal_analytics_is_protected_and_health_is_public() -> None:
    coordinator = FakeCoordinator()
    async with await _client(coordinator) as client:
        health = await client.get("/healthz")
        assert health.status_code == 200
        assert health.headers["x-correlation-id"]

        unauthorized = await client.get("/api/v1/internal/analytics")
        assert unauthorized.status_code == 401

        analytics = await client.get(
            "/api/v1/internal/analytics",
            headers={"Authorization": "Bearer internal"},
        )
        assert analytics.status_code == 200
        assert analytics.json()["total_screenings"] == 0


@pytest.mark.asyncio
async def test_internal_summary_retry_requires_key_and_serializes_success() -> None:
    coordinator = FakeCoordinator()
    async with await _client(coordinator) as client:
        missing = await client.post("/api/v1/internal/screenings/session-1/summary/retry")
        assert missing.status_code == 401
        assert missing.json()["error"]["code"] == "missing_bearer_token"

        invalid = await client.post(
            "/api/v1/internal/screenings/session-1/summary/retry",
            headers={"Authorization": "Bearer wrong"},
        )
        assert invalid.status_code == 401
        assert invalid.json()["error"]["code"] == "invalid_internal_key"

        success = await client.post(
            "/api/v1/internal/screenings/session-1/summary/retry",
            headers={"Authorization": "Bearer internal"},
        )
        assert success.status_code == 200
        assert success.json()["summary"] == "Repaired summary"
        assert success.json()["summary_status"] == "generated"
