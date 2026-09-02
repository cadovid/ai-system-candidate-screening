#!/usr/bin/env python3
"""Run the bounded paused-conversation reminder job.

Examples::

    python scripts/reengage.py --dry-run
    python scripts/reengage.py --apply --database-url sqlite+aiosqlite:///./var/screening.db

The command prints one JSON object so it is safe to use from cron.  The
default is dry-run; ``--apply`` is an explicit opt-in to database writes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# Make direct ``python scripts/reengage.py`` work from a source checkout.
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from candidate_screening.application.reengagement import ReengagementService  # noqa: E402
from candidate_screening.config import Settings  # noqa: E402
from candidate_screening.persistence import Base, SqlAlchemyUnitOfWork  # noqa: E402
from candidate_screening.persistence.database import (  # noqa: E402
    create_async_engine_for_url,
    create_session_factory,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Queue at most one reminder for inactive screenings"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="show eligible conversations without writing (default)",
    )
    mode.add_argument("--apply", action="store_true", help="persist reminders")
    parser.add_argument(
        "--database-url", default=None, help="database URL; defaults to DATABASE_URL/settings"
    )
    parser.add_argument("--inactivity-hours", type=float, default=None, help="minimum idle period")
    parser.add_argument(
        "--max-reminders", type=int, default=1, help="maximum reminders per conversation"
    )
    parser.add_argument("--limit", type=int, default=500, help="maximum conversations to inspect")
    parser.add_argument(
        "--now", default=None, help="as-of UTC timestamp (ISO-8601), useful for repeatable runs"
    )
    parser.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="do not create missing tables (normally useful for a fresh local demo database)",
    )
    return parser


def _as_of(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


async def _run(args: argparse.Namespace) -> int:
    settings = Settings(database_url=args.database_url) if args.database_url else Settings()
    database_url = args.database_url or settings.database_url
    engine = create_async_engine_for_url(database_url)
    try:
        if not args.no_bootstrap:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        factory = create_session_factory(engine)
        service = ReengagementService(
            lambda: SqlAlchemyUnitOfWork(factory),
            inactivity_hours=(args.inactivity_hours or settings.inactivity_hours),
            max_reminders=args.max_reminders,
        )
        report = await service.run(
            dry_run=not args.apply,
            now=_as_of(args.now),
            limit=args.limit,
        )
        print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
