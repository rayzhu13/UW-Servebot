# ServeBot

Discord bot: `@mention` it with a freeform description of who owns what and
when it's due; it parses the tasks with the Anthropic API, asks you to
confirm, then pings assignees in-channel when reminders come due. You can
also `@mention` it to say a task is done, or to edit an existing task
("push alice's slides to friday", "reassign the invoices to bob"). Any
server member can create, close, or edit tasks — there's no admin gate on
any of it.

**Just want to use the bot?** See [USAGE.md](USAGE.md). The rest of this
file is setup and implementation notes.

## What's built (MVP, per plan §10, plus one free stretch item)

- [x] `@ServeBot` mention trigger (§3) — open to any server member, not
      admin-gated (deliberately opened up; see §12 below)
- [x] LLM-based parse -> confirmation (reactions) -> commit (§4)
- [x] Every task always gets exactly two notifications: one reminder before
      the due time, and one at the due time itself, both pinging the
      assignee (§6/§7). The reminder offset is per-task if the message
      specifies one (e.g. "remind her a day ahead"), otherwise it falls
      back to the server's default (`/servebot set-default-reminder`,
      30 minutes before due out of the box).
- [x] Channel-ping delivery (§7). Per-task channel routing, in priority
      order: (1) a channel mentioned in the message itself for that task
      (e.g. "post this in #updates"), (2) a per-origin-channel override
      configured for the channel the task was created in
      (`/servebot set-reminder-channel`, run in that origin channel),
      (3) the channel the task was created in. The confirmation embed shows
      which channel and exact reminder time each task will use before you
      commit.
- [x] Postgres polling scheduler (§8, MVP approach)
- [x] Self-service task completion: `@mention` the bot saying a task is done
      and it'll mark it `status = 'done'` (kept, not deleted) after the usual
      ✅ confirmation. Any member can close any open task.
- [x] Natural-language task editing: `@mention` the bot to change a task's
      due date, assignee, description, and/or reminder time
      (`parsing/parser.py`'s `edits` extraction + `db.update_task`/
      `db.replace_reminders`). An edit only applies if every field it asks
      to change resolves successfully — otherwise the whole edit is
      flagged and skipped in confirmation, same as an unresolved new task.
      Changing the due date (or reminder time) regenerates that task's
      reminder + due-time notification around the new due date.
- [x] `/servebot set-reminder-channel`, `/servebot set-default-reminder`,
      `/servebot list`, `/servebot cancel-task` (also nominally stretch,
      but small; `cancel-task` still requires Manage Server)

## Not built yet (still genuinely stretch, per plan §10)

- Redis-backed scheduling queue (§8 v2) — table scan is fine at this scale
- Recurring tasks
- Web view of open tasks
- The bot/FastAPI split (§9) — running single-process for now; revisit if
  you need a web view or a second surface for tasks

## Open questions from the plan (§12) — decisions made, revisit if wrong

1. **Ambiguous assignees:** flagged individually, rest of the batch still
   commits. Change in `bot/discord_bot.py::_commit_batch` if you'd rather
   block the whole batch.
2. **`/servebot list`:** built — scoped to the current channel.
3. **Timezone:** everything currently assumes UTC end-to-end (see
   `on_message` in `bot/discord_bot.py`, `timezone_name="UTC"`). This is the
   biggest thing worth revisiting before real use — "friday at 3pm" parsed
   in UTC will be wrong for anyone not in UTC. Two reasonable fixes: a
   per-user `/servebot set-timezone` command, or just assuming a single
   server-wide timezone via `/servebot set-default-reminder`-style config.
   Not built yet either way.
4. **Who can create/close/edit tasks:** originally admin-only (Manage Server
   or a configured admin role), later opened up so any server member can
   create, close, or edit any open task. There's no more permission check
   in `on_message` — if you want to restrict this again later, that's the
   place to add it back.

## Setup

1. **Discord application:** create one at
   https://discord.com/developers/applications, add a bot user, enable the
   **Message Content** and **Server Members** privileged intents, and
   invite it to your server with `applications.commands` + `bot` scopes and
   at least Send Messages / Add Reactions / Read Message History
   permissions.

2. **Postgres:** create a database, then run:
   ```bash
   psql "$DATABASE_URL" -f db/schema.sql
   ```

3. **Environment:**
   ```bash
   cp .env.example .env
   # fill in DISCORD_BOT_TOKEN, ANTHROPIC_API_KEY, DATABASE_URL
   ```

4. **Install and run:**
   ```bash
   pip install -r requirements.txt
   python main.py
   ```

## Try it

In a channel the bot can see:

> @ServeBot alice needs to get the slides finalized by friday at 3pm, bob
> has to send out the invoices by end of day tomorrow, and carol should
> review PR 42 sometime before next monday morning, remind her a day ahead
> too

React ✅ on the confirmation the bot posts, and it'll schedule the
reminders.

## Layout

```
config.py               env-driven config
main.py                 entry point
db/
  schema.sql             plan §5, plus guild_settings
  db.py                  asyncpg data-access layer
parsing/
  parser.py              plan §4 — LLM extraction, normalization, assignee resolution
bot/
  discord_bot.py          mention trigger, confirmation flow, scheduler, slash commands
```
