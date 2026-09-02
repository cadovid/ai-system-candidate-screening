"""Correlation, body-size, rate, and provider-concurrency middleware."""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import cast

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .limits import SlidingWindowRateLimiter, TurnConcurrencyLimit

logger = logging.getLogger(__name__)
_CORRELATION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


def _error_payload(code: str, message: str, correlation_id: str) -> bytes:
    return json.dumps(
        {"error": {"code": code, "message": message, "correlation_id": correlation_id}},
        ensure_ascii=False,
    ).encode("utf-8")


async def _send_json(
    send: Send,
    status_code: int,
    body: bytes,
    correlation_id: str,
    *,
    retry_after: int | None = None,
) -> None:
    headers = [
        (b"content-type", b"application/json"),
        (b"x-correlation-id", correlation_id.encode()),
    ]
    if retry_after is not None:
        headers.append((b"retry-after", str(retry_after).encode()))
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class CorrelationMiddleware:
    """Attach a bounded correlation ID and turn unexpected errors into JSON."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        supplied = headers.get(b"x-correlation-id", b"").decode("latin-1")
        correlation_id = supplied if _CORRELATION_RE.fullmatch(supplied) else str(uuid.uuid4())
        state = scope.setdefault("state", {})
        state["correlation_id"] = correlation_id
        sent_start = False

        async def send_with_correlation(message: Message) -> None:
            nonlocal sent_start
            if message.get("type") == "http.response.start":
                sent_start = True
                response_headers = list(message.get("headers", []))
                if not any(key.lower() == b"x-correlation-id" for key, _ in response_headers):
                    response_headers.append((b"x-correlation-id", correlation_id.encode("ascii")))
                message = {**message, "headers": response_headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_correlation)
        except Exception:
            logger.exception("unhandled HTTP error", extra={"correlation_id": correlation_id})
            if not sent_start:
                await _send_json(
                    send,
                    500,
                    _error_payload(
                        "internal_error", "An unexpected error occurred.", correlation_id
                    ),
                    correlation_id,
                )


class RequestLimitsMiddleware:
    """Reject oversized/rate-limited requests before JSON parsing or model work."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int = 32_000,
        rate_limiter: SlidingWindowRateLimiter | None = None,
        turn_concurrency: TurnConcurrencyLimit | None = None,
    ) -> None:
        if max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.rate_limiter = rate_limiter
        self.turn_concurrency = turn_concurrency

    @staticmethod
    def _is_candidate(scope: Scope) -> bool:
        path = str(scope.get("path", ""))
        return "/candidate/" in path or path.startswith("/candidate")

    @staticmethod
    def _is_turn(scope: Scope) -> bool:
        return RequestLimitsMiddleware._is_candidate(scope) and "/turn" in str(
            scope.get("path", "")
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length", b"")
        try:
            declared_size = int(content_length) if content_length else 0
        except ValueError:
            declared_size = self.max_body_bytes + 1
        correlation_id = str(scope.get("state", {}).get("correlation_id", "")) or str(uuid.uuid4())
        if declared_size > self.max_body_bytes:
            await _send_json(
                send,
                413,
                _error_payload(
                    "request_too_large", "The request body is too large.", correlation_id
                ),
                correlation_id,
            )
            return

        key = self._client_key(scope)
        if self.rate_limiter is not None and self._is_candidate(scope):
            rate = self.rate_limiter.check(key)
            if not rate.allowed:
                await _send_json(
                    send,
                    429,
                    _error_payload(
                        "rate_limited", "Too many requests. Please try again later.", correlation_id
                    ),
                    correlation_id,
                    retry_after=rate.retry_after,
                )
                return

        body_size = 0

        async def receive_limited() -> Message:
            nonlocal body_size
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                body_size += len(body)
                if body_size > self.max_body_bytes:
                    raise _RequestTooLarge
            return message

        acquired = False
        if self.turn_concurrency is not None and self._is_turn(scope):
            acquired = await self.turn_concurrency.acquire()
            if not acquired:
                await _send_json(
                    send,
                    429,
                    _error_payload(
                        "concurrency_limited",
                        "Too many screening requests are in progress.",
                        correlation_id,
                    ),
                    correlation_id,
                    retry_after=1,
                )
                return
        try:
            await self.app(scope, receive_limited, send)
        except _RequestTooLarge:
            await _send_json(
                send,
                413,
                _error_payload(
                    "request_too_large", "The request body is too large.", correlation_id
                ),
                correlation_id,
            )
        finally:
            if acquired and self.turn_concurrency is not None:
                self.turn_concurrency.release()

    @staticmethod
    def _client_key(scope: Scope) -> str:
        client = scope.get("client")
        if isinstance(client, tuple) and client:
            client_values = cast(tuple[object, ...], client)
            return str(client_values[0])
        return "unknown"


class _RequestTooLarge(Exception):
    pass


__all__ = ["CorrelationMiddleware", "RequestLimitsMiddleware"]
