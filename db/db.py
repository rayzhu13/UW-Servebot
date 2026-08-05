"""
Thin async data-access layer over Postgres (asyncpg). No ORM — the schema
is simple enough (per plan §5) that raw SQL is clearer than adding a
dependency like SQLAlchemy for this project's size.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import List, Optional

import asyncpg

_pool: Optional[asyncpg.Pool] = None


async def init_pool(database_url: str) -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=5)
    return _pool


async def close_pool() -> None:
    if _pool is not None:
        await _pool.close()


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized — call init_pool() first")
    return _pool


@dataclass
class GuildSettings:
    guild_id: int
    default_reminder_minutes: int
    timezone_name: str
    reminder_channel_id: Optional[int]


async def get_guild_settings(guild_id: int) -> GuildSettings:
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT guild_id, default_reminder_minutes, timezone_name, reminder_channel_id "
        "FROM guild_settings WHERE guild_id = $1",
        guild_id,
    )
    if row is None:
        # No row yet — insert one with defaults so future reads/writes are simple.
        row = await pool.fetchrow(
            "INSERT INTO guild_settings (guild_id) VALUES ($1) "
            "ON CONFLICT (guild_id) DO NOTHING "
            "RETURNING guild_id, default_reminder_minutes, timezone_name, reminder_channel_id",
            guild_id,
        )
        if row is None:
            row = await pool.fetchrow(
                "SELECT guild_id, default_reminder_minutes, timezone_name, reminder_channel_id "
                "FROM guild_settings WHERE guild_id = $1",
                guild_id,
            )
    return GuildSettings(
        guild_id=row["guild_id"],
        default_reminder_minutes=row["default_reminder_minutes"],
        timezone_name=row["timezone_name"],
        reminder_channel_id=row["reminder_channel_id"],
    )


async def set_reminder_channel(guild_id: int, channel_id: Optional[int]) -> None:
    pool = _get_pool()
    await pool.execute(
        "INSERT INTO guild_settings (guild_id, reminder_channel_id) VALUES ($1, $2) "
        "ON CONFLICT (guild_id) DO UPDATE SET reminder_channel_id = EXCLUDED.reminder_channel_id",
        guild_id,
        channel_id,
    )


async def set_default_reminder_minutes(guild_id: int, minutes: int) -> None:
    pool = _get_pool()
    await pool.execute(
        "INSERT INTO guild_settings (guild_id, default_reminder_minutes) VALUES ($1, $2) "
        "ON CONFLICT (guild_id) DO UPDATE SET default_reminder_minutes = EXCLUDED.default_reminder_minutes",
        guild_id,
        minutes,
    )


async def set_timezone(guild_id: int, timezone_name: str) -> None:
    pool = _get_pool()
    await pool.execute(
        "INSERT INTO guild_settings (guild_id, timezone_name) VALUES ($1, $2) "
        "ON CONFLICT (guild_id) DO UPDATE SET timezone_name = EXCLUDED.timezone_name",
        guild_id,
        timezone_name,
    )


@dataclass
class NewTask:
    guild_id: int
    channel_id: int
    created_by: int
    assignee_id: int
    description: str
    due_at: dt.datetime
    offsets_minutes: List[int]


async def create_task_with_reminders(task: NewTask) -> int:
    """Insert a task and its reminders in one transaction. Returns task id."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            task_id = await conn.fetchval(
                """
                INSERT INTO tasks (guild_id, channel_id, created_by, assignee_id, description, due_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                task.guild_id,
                task.channel_id,
                task.created_by,
                task.assignee_id,
                task.description,
                task.due_at,
            )
            for minutes_before in task.offsets_minutes:
                remind_at = task.due_at - dt.timedelta(minutes=minutes_before)
                label = "at_due" if minutes_before == 0 else f"{minutes_before}_min_before"
                await conn.execute(
                    """
                    INSERT INTO reminders (task_id, remind_at, label)
                    VALUES ($1, $2, $3)
                    """,
                    task_id,
                    remind_at,
                    label,
                )
    return task_id


@dataclass
class DueReminder:
    reminder_id: int
    task_id: int
    guild_id: int
    channel_id: int
    assignee_id: int
    description: str
    due_at: dt.datetime
    label: Optional[str]


async def get_due_reminders(now: Optional[dt.datetime] = None) -> List[DueReminder]:
    pool = _get_pool()
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = await pool.fetch(
        """
        SELECT r.id AS reminder_id, r.task_id, r.label,
               t.guild_id, t.channel_id, t.assignee_id, t.description, t.due_at
        FROM reminders r
        JOIN tasks t ON t.id = r.task_id
        WHERE r.remind_at <= $1 AND r.sent = false AND t.status = 'pending'
        ORDER BY r.remind_at ASC
        """,
        now,
    )
    return [
        DueReminder(
            reminder_id=row["reminder_id"],
            task_id=row["task_id"],
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            assignee_id=row["assignee_id"],
            description=row["description"],
            due_at=row["due_at"],
            label=row["label"],
        )
        for row in rows
    ]


async def mark_reminder_sent(reminder_id: int) -> None:
    pool = _get_pool()
    await pool.execute("UPDATE reminders SET sent = true WHERE id = $1", reminder_id)


async def list_open_tasks(guild_id: int, channel_id: Optional[int] = None) -> List[dict]:
    pool = _get_pool()
    if channel_id is not None:
        rows = await pool.fetch(
            "SELECT * FROM tasks WHERE guild_id = $1 AND channel_id = $2 AND status = 'pending' "
            "ORDER BY due_at ASC",
            guild_id,
            channel_id,
        )
    else:
        rows = await pool.fetch(
            "SELECT * FROM tasks WHERE guild_id = $1 AND status = 'pending' ORDER BY due_at ASC",
            guild_id,
        )
    return [dict(row) for row in rows]


async def cancel_task(task_id: int, guild_id: int) -> bool:
    pool = _get_pool()
    result = await pool.execute(
        "UPDATE tasks SET status = 'cancelled' WHERE id = $1 AND guild_id = $2",
        task_id,
        guild_id,
    )
    return result.endswith("1")


async def complete_task(task_id: int, guild_id: int) -> bool:
    pool = _get_pool()
    result = await pool.execute(
        "UPDATE tasks SET status = 'done' WHERE id = $1 AND guild_id = $2",
        task_id,
        guild_id,
    )
    return result.endswith("1")


async def get_task(task_id: int, guild_id: int) -> Optional[dict]:
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM tasks WHERE id = $1 AND guild_id = $2 AND status = 'pending'",
        task_id,
        guild_id,
    )
    return dict(row) if row else None


async def update_task(
    task_id: int,
    guild_id: int,
    description: Optional[str] = None,
    due_at: Optional[dt.datetime] = None,
    assignee_id: Optional[int] = None,
) -> bool:
    """Update only the given (non-None) fields on a pending task."""
    pool = _get_pool()
    result = await pool.execute(
        """
        UPDATE tasks SET
            description = COALESCE($3, description),
            due_at = COALESCE($4, due_at),
            assignee_id = COALESCE($5, assignee_id)
        WHERE id = $1 AND guild_id = $2 AND status = 'pending'
        """,
        task_id,
        guild_id,
        description,
        due_at,
        assignee_id,
    )
    return result.endswith("1")


async def get_active_reminder_offsets_minutes(task_id: int) -> List[int]:
    """Minutes-before-due for each of a task's not-yet-sent reminders, derived
    from (due_at - remind_at) rather than parsed out of the label string."""
    pool = _get_pool()
    rows = await pool.fetch(
        """
        SELECT EXTRACT(EPOCH FROM (t.due_at - r.remind_at)) / 60 AS minutes_before
        FROM reminders r
        JOIN tasks t ON t.id = r.task_id
        WHERE r.task_id = $1 AND r.sent = false
        """,
        task_id,
    )
    return [int(round(row["minutes_before"])) for row in rows]


async def replace_reminders(task_id: int, due_at: dt.datetime, offsets_minutes: List[int]) -> None:
    """Drop a task's unsent reminders and recreate them relative to due_at —
    used when an edit changes the due date and/or reminder offset."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM reminders WHERE task_id = $1 AND sent = false", task_id)
            for minutes_before in offsets_minutes:
                remind_at = due_at - dt.timedelta(minutes=minutes_before)
                label = "at_due" if minutes_before == 0 else f"{minutes_before}_min_before"
                await conn.execute(
                    "INSERT INTO reminders (task_id, remind_at, label) VALUES ($1, $2, $3)",
                    task_id,
                    remind_at,
                    label,
                )
