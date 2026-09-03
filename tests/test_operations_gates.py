from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from candidate_screening.application.coordinator import TurnCoordinator
from candidate_screening.config import Settings
from candidate_screening.main import create_app
from candidate_screening.observability import PiiSafeJsonFormatter
from candidate_screening.persistence import Base
from candidate_screening.persistence.database import create_async_engine_for_url

DATA_ROOT = Path(__file__).parents[1] / "data"


@pytest.mark.asyncio
async def test_livez_is_static_and_readyz_checks_database_and_fixtures() -> None:
    app = create_app(
        settings=Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            service_areas_path=DATA_ROOT / "service_areas" / "service_areas.json",
            faq_path=DATA_ROOT / "faq" / "faq.json",
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        live = await client.get("/livez")
        ready = await client.get("/readyz")

    assert live.status_code == 200
    assert live.json() == {"status": "alive"}
    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "checks": {"database": "ok", "fixtures": "ok"},
    }


@pytest.mark.asyncio
async def test_readyz_reports_fixture_failure_without_hiding_database_status(
    tmp_path: Path,
) -> None:
    app = create_app(
        settings=Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            service_areas_path=tmp_path / "missing-service-areas.json",
            faq_path=DATA_ROOT / "faq" / "faq.json",
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "ok", "fixtures": "failed"},
    }


@pytest.mark.asyncio
async def test_language_switch_post_and_reload_serialize_real_decision(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'language-switch.db'}"
    engine = create_async_engine_for_url(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    settings = Settings(
        database_url=database_url,
        service_areas_path=DATA_ROOT / "service_areas" / "service_areas.json",
        faq_path=DATA_ROOT / "faq" / "faq.json",
    )
    app = create_app(settings=settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/api/v1/candidate/conversations", json={"language": "es", "channel": "web"}
        )
        assert created.status_code == 201
        created_payload = created.json()
        authorization = {"Authorization": f"Bearer {created_payload['resume_token']}"}
        switched = await client.post(
            f"/api/v1/candidate/conversations/{created_payload['conversation_id']}/turns",
            headers={**authorization, "Idempotency-Key": "language-switch-test"},
            json={"message": "English please", "language": "en"},
        )
        assert switched.status_code == 200
        assert switched.json()["decision"]["status"] == "in_progress"

    # A fresh app instance represents a process reload.  The persisted
    # preferred language must remain English and decision serialization must
    # still satisfy the strict DTO.
    reloaded_app = create_app(settings=settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=reloaded_app), base_url="http://test"
    ) as client:
        restored = await client.get(
            f"/api/v1/candidate/conversations/{created_payload['conversation_id']}",
            headers=authorization,
        )
    assert restored.status_code == 200
    assert restored.json()["language"] == "en"
    assert restored.json()["decision"]["status"] == "in_progress"
    await engine.dispose()


def test_logging_formatter_hashes_identifiers_and_redacts_untrusted_text() -> None:
    import logging

    record = logging.LogRecord(
        name="test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="candidate email alice@example.test shared password hunter2",
        args=(),
        exc_info=None,
    )
    record.conversation_id = "conversation-secret"
    record.llm_provider = "groq"
    record.required_environment_variable = "GROQ_API_KEY"
    record.provider_error_category = "rate_limited"
    rendered = PiiSafeJsonFormatter().format(record)
    payload = json.loads(rendered)

    assert "alice@example.test" not in rendered
    assert "hunter2" not in rendered
    assert payload["conversation_id"] == hashlib.sha256(b"conversation-secret").hexdigest()[:16]
    assert payload["llm_provider"] == "groq"
    assert payload["required_environment_variable"] == "GROQ_API_KEY"
    assert payload["provider_error_category"] == "rate_limited"


@pytest.mark.asyncio
async def test_summary_persistence_failure_is_non_fatal() -> None:
    class BrokenUOW:
        async def __aenter__(self) -> object:
            raise RuntimeError("database unavailable with secret=do-not-log")

        async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
            return None

    coordinator = TurnCoordinator(
        cast(Any, lambda: BrokenUOW()),
        cast(Any, None),
        cast(Any, None),
    )

    persisted = await cast(Any, coordinator)._persist_summary(
        "turn-1", "session-1", "safe summary", "fallback"
    )
    assert persisted is False
