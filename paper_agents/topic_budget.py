"""Durable, serialized Topic Agent allowance ledger."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
DEFAULT_LIMIT = 400


def _day(now: datetime | None = None) -> str:
    return (now or datetime.now(PACIFIC)).astimezone(PACIFIC).date().isoformat()


def ensure_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS topic_agent_allowance (
            allowance_day TEXT PRIMARY KEY,
            limit_count INTEGER NOT NULL,
            reserved_count INTEGER NOT NULL DEFAULT 0,
            consumed_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )


def reserve(connection: sqlite3.Connection, *, limit: int = DEFAULT_LIMIT,
            now: datetime | None = None) -> dict[str, int | str | bool]:
    if limit < 1:
        raise ValueError("limit must be positive")
    day = _day(now)
    ensure_table(connection)
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "INSERT INTO topic_agent_allowance(allowance_day, limit_count) VALUES (?, ?) "
            "ON CONFLICT(allowance_day) DO UPDATE SET limit_count=excluded.limit_count",
            (day, limit),
        )
        row = connection.execute(
            "SELECT limit_count, reserved_count, consumed_count FROM topic_agent_allowance WHERE allowance_day=?",
            (day,),
        ).fetchone()
        used = int(row[1]) + int(row[2])
        if used >= int(row[0]):
            connection.rollback()
            return {"day": day, "limit": int(row[0]), "remaining": 0, "reserved": False}
        connection.execute(
            "UPDATE topic_agent_allowance SET reserved_count=reserved_count+1, updated_at=datetime('now') WHERE allowance_day=?",
            (day,),
        )
        connection.commit()
        return {"day": day, "limit": int(row[0]), "remaining": int(row[0]) - used - 1, "reserved": True}
    except Exception:
        connection.rollback()
        raise


def finish(connection: sqlite3.Connection, *, day: str, succeeded: bool) -> None:
    ensure_table(connection)
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "UPDATE topic_agent_allowance SET reserved_count=MAX(0,reserved_count-1), consumed_count=consumed_count+1, updated_at=datetime('now') WHERE allowance_day=?",
            (day,),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def status(connection: sqlite3.Connection, *, limit: int = DEFAULT_LIMIT,
           now: datetime | None = None) -> dict[str, int | str]:
    ensure_table(connection)
    day = _day(now)
    row = connection.execute("SELECT limit_count,reserved_count,consumed_count FROM topic_agent_allowance WHERE allowance_day=?", (day,)).fetchone()
    if row is None:
        return {"day": day, "limit": limit, "remaining": limit}
    return {"day": day, "limit": int(row[0]), "remaining": max(0, int(row[0]) - int(row[1]) - int(row[2]))}
