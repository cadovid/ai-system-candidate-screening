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
    InteractionMode,
    Language,
    ScreeningStatus,
    TurnStatus,
)
from candidate_screening.domain.models import ScreeningState
from candidate_screening.main import create_app


class FakeCoordinator:
    def __init__(self) -> None:
        self.turn_keys: list[str] = []
        self.turn_modes: list[InteractionMode] = []

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
        language: Language | None = None,
        input_mode: InteractionMode = InteractionMode.TEXT,
    ) -> TurnCoordinatorResult:
        _ = (message, correlation_id, language)
        self.turn_keys.append(idempotency_key)
        self.turn_modes.append(input_mode)
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
        assert coordinator.turn_modes == [InteractionMode.TEXT]

        voice_turn = await client.post(
            "/api/v1/candidate/conversations/conversation-1/turns",
            headers={"Authorization": "Bearer resume-1", "Idempotency-Key": "voice-key"},
            json={"message": "Madrid centro", "input_mode": "voice"},
        )
        assert voice_turn.status_code == 200
        assert coordinator.turn_modes[-1] is InteractionMode.VOICE

        invalid_mode = await client.post(
            "/api/v1/candidate/conversations/conversation-1/turns",
            headers={"Authorization": "Bearer resume-1", "Idempotency-Key": "bad-mode"},
            json={"message": "Madrid centro", "input_mode": "audio"},
        )
        assert invalid_mode.status_code == 422


@pytest.mark.asyncio
async def test_favicon_is_served() -> None:
    async with await _client(FakeCoordinator()) as client:
        response = await client.get("/favicon.ico")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.content.startswith(b"<svg")


@pytest.mark.asyncio
async def test_analytics_dashboard_is_a_shell_and_json_api_remains_protected() -> None:
    coordinator = FakeCoordinator()
    async with await _client(coordinator) as client:
        candidate = await client.get("/")
        recruiter = await client.get("/recruiter")
        dashboard = await client.get("/analytics", headers={"Accept": "text/html"})
        assert candidate.status_code == 200
        assert b'class="candidate-page"' in candidate.content
        assert recruiter.status_code == 200
        assert b'class="recruiter-page"' in recruiter.content
        assert dashboard.status_code == 200
        assert dashboard.headers["content-type"].startswith("text/html")
        assert b"Screening analytics" in dashboard.content
        assert b"/static/analytics.js" in dashboard.content

        root_json_alias = await client.get("/analytics", headers={"Accept": "application/json"})
        assert root_json_alias.status_code == 401

        authorized_root_alias = await client.get(
            "/analytics",
            headers={"Accept": "application/json", "Authorization": "Bearer internal"},
        )
        assert authorized_root_alias.status_code == 200
        assert authorized_root_alias.json()["total_screenings"] == 0

        json_alias = await client.get("/api/v1/analytics")
        assert json_alias.status_code == 401

        authorized_alias = await client.get(
            "/api/v1/analytics",
            headers={"Authorization": "Bearer internal"},
        )
        assert authorized_alias.status_code == 200
        assert authorized_alias.json()["total_screenings"] == 0


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
