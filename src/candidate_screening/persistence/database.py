"""Async SQLAlchemy database setup."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any, cast

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def _install_threadsafe_wakeup_fallback(loop: asyncio.AbstractEventLoop) -> None:
    """Make cross-thread asyncio callbacks work in restricted runtimes.

    ``aiosqlite`` executes SQLite calls on a worker thread and wakes the event
    loop with its self-pipe.  A few managed/container runtimes deny Unix socket
    ``send`` (the normal asyncio self-pipe), which leaves every DB await stuck
    until the next unrelated timer.  A local OS pipe uses the same selector
    machinery without relying on socket send and is safe to use as a fallback.
    The probe is deliberately best-effort and leaves normal event loops alone.
    """

    if getattr(loop, "_candidate_screening_pipe_wakeup", False):
        return
    csock = getattr(loop, "_csock", None)
    if csock is None:
        return
    try:
        csock.send(b"\0")
        return
    except OSError:
        pass

    try:
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)
    except OSError:
        return

    def drain_pipe() -> None:
        try:
            while os.read(read_fd, 4096):
                pass
        except BlockingIOError, OSError:
            pass

    try:
        loop.add_reader(read_fd, drain_pipe)
    except AttributeError, RuntimeError, OSError:
        os.close(read_fd)
        os.close(write_fd)
        return

    original_close: Callable[..., Any] | None = getattr(loop, "_candidate_screening_close", None)
    if original_close is None:
        original_close = loop.close

        def close_with_pipe(*args: Any, **kwargs: Any) -> Any:
            with suppress(AttributeError, RuntimeError, OSError):
                loop.remove_reader(read_fd)
            for descriptor in (read_fd, write_fd):
                with suppress(OSError):
                    os.close(descriptor)
            return original_close(*args, **kwargs)

        loop_any = cast(Any, loop)
        loop_any._candidate_screening_close = original_close
        loop.close = close_with_pipe  # type: ignore[method-assign]

    def write_to_pipe() -> None:
        with suppress(OSError):
            os.write(write_fd, b"\0")

    # ``call_soon_threadsafe`` invokes this private hook after putting the
    # callback on the loop's ready queue.  The pipe only replaces the wakeup;
    # callback ordering and thread-safety remain asyncio's responsibility.
    loop_any = cast(Any, loop)
    loop_any._write_to_self = write_to_pipe
    loop_any._candidate_screening_pipe_wakeup = True


def _ensure_event_loop_wakeup() -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _install_threadsafe_wakeup_fallback(loop)


def create_async_engine_for_url(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create a configured async engine for SQLite or a PostgreSQL-compatible URL."""

    _ensure_event_loop_wakeup()

    # Alembic's default URL and many local test fixtures use the synchronous
    # ``sqlite:///`` spelling.  The application always needs an async driver,
    # so normalize it at this boundary rather than making every caller know
    # the driver detail.
    if database_url.startswith("sqlite:///"):
        database_url = database_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    elif database_url.startswith("sqlite+") and "+aiosqlite" not in database_url:
        database_url = "sqlite+aiosqlite://" + database_url.split("://", 1)[1]
    connect_args: dict[str, Any] = {}
    if database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False, "timeout": 30}

    engine = create_async_engine(
        database_url, echo=echo, pool_pre_ping=True, connect_args=connect_args
    )

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    _ensure_event_loop_wakeup()
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession]:
    """Yield a short-lived session and rollback on an unhandled error."""

    async with session_factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        else:
            await session.commit()
