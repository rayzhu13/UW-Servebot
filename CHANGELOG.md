# Changelog

## v1.08

- Unhandled errors while processing an @mention (LLM/API failures, DB
  hiccups, etc.) no longer fail silently. `on_message` now catches any
  exception around the parse step, logs the full traceback, and replies
  in-channel so a failure is visible instead of leaving the bot looking
  unresponsive.

## v1.07

- `/servebot set-reminder-channel` is now **per-channel** instead of
  server-wide. Run it in the channel whose tasks you want redirected —
  it only affects tasks created in that channel, not the whole server.
  Other channels keep posting reminders in themselves unless they get
  their own override.
- Removed the old single guild-wide reminder-channel setting
  (`guild_settings.reminder_channel_id`) in favor of a new
  `channel_settings` table keyed by `(guild_id, channel_id)`.
- Clearing an override (`/servebot set-reminder-channel` with no channel
  argument) now clears it only for the channel you run it in.
