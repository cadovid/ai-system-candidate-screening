"""Alembic environment for the async application database."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from alembic import context
from sqlalchemy.engine import Connection

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from candidate_screening.config import get_settings  # noqa: E402
from candidate_screening.persistence.database import create_async_engine_for_url  # noqa: E402
from candidate_screening.persistence.orm import Base  # noqa: E402

config = context.config
settings = get_settings()
database_url = settings.database_url
if database_url.startswith("sqlite:///") and "+aiosqlite" not in database_url:
    database_url = database_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
target_metadata = Base.metadata


def _ensure_sqlite_parent(url: str) -> None:
    """Create a local SQLite parent directory before Alembic opens it."""

    if not url.startswith("sqlite") or "///" not in url:
        return
    database_path = url.split("///", 1)[1].split("?", 1)[0]
    if not database_path or database_path == ":memory:":
        return
    path = Path(database_path)
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    _ensure_sqlite_parent(database_url)
    connectable = create_async_engine_for_url(database_url)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
