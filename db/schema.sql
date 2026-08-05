-- ServeBot schema
-- Run with: psql $DATABASE_URL -f db/schema.sql

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id BIGINT PRIMARY KEY,
    default_reminder_minutes INTEGER NOT NULL DEFAULT 30,  -- used when a task doesn't specify its own reminder time
    timezone_name TEXT NOT NULL DEFAULT 'America/Toronto',  -- IANA name
    reminder_channel_id BIGINT                      -- NULL => use the channel the task was created in
);

-- Safe to re-run on an existing database: adds the column if it's missing.
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS timezone_name TEXT NOT NULL DEFAULT 'America/Toronto';
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS reminder_channel_id BIGINT;
-- Task creation/closing opened up to any server member — admin_role_id is no longer read anywhere.
ALTER TABLE guild_settings DROP COLUMN IF EXISTS admin_role_id;
-- Replaced the arbitrary default_offsets_minutes[] list with a single default
-- reminder-before-due value; every task now always gets exactly a reminder +
-- a due-time notification (see bot/discord_bot.py::_commit_batch).
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS default_reminder_minutes INTEGER NOT NULL DEFAULT 30;
ALTER TABLE guild_settings DROP COLUMN IF EXISTS default_offsets_minutes;

CREATE TABLE IF NOT EXISTS tasks (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    created_by BIGINT NOT NULL,
    assignee_id BIGINT NOT NULL,
    description TEXT NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', -- pending | done | cancelled
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reminders (
    id SERIAL PRIMARY KEY,
    task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    remind_at TIMESTAMPTZ NOT NULL,
    label TEXT, -- e.g. '1_day_before', 'at_due'
    sent BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_reminders_pending ON reminders (remind_at) WHERE sent = false;
CREATE INDEX IF NOT EXISTS idx_tasks_guild_channel ON tasks (guild_id, channel_id);
