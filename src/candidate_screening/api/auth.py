"""Authentication helpers for opaque candidate and internal bearer keys."""

from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException, Request, status
from pydantic import SecretStr

from candidate_screening.application.coordinator import TurnCoordinator


def bearer_token(request: Request) -> str:
    """Extract exactly one bearer credential without logging its value."""

    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.casefold() != "bearer" or not value.strip() or " " in value.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "missing_bearer_token", "message": "A bearer token is required."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return value.strip()


async def require_candidate_token(
    request: Request, conversation_id: str, coordinator: TurnCoordinator
) -> str:
    token = bearer_token(request)
    if not await coordinator.verify_resume_token(conversation_id, token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_resume_token", "message": "The resume token is invalid."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def _secret_value(value: SecretStr | str | None) -> str:
    if value is None:
        return ""
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def require_internal_key(request: Request, expected: SecretStr | str | None) -> str:
    """Require a separately configured internal key; never fall back to resume tokens."""

    supplied = bearer_token(request)
    expected_value = _secret_value(expected)
    if not expected_value or not hmac.compare_digest(supplied, expected_value):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_internal_key", "message": "An internal API key is required."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return supplied


def hash_for_log(value: str) -> str:
    """Return a non-reversible correlation-safe identifier for diagnostics."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


__all__ = ["bearer_token", "hash_for_log", "require_candidate_token", "require_internal_key"]
