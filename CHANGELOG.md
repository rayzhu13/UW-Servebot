# Changelog

## v1.09

- Fixed a day-of-week resolution bug where a message sent in the evening
  (guild-local time) could get its due date parsed one day early — e.g.
  "thursday at 9pm" landing on Wednesday. The reference weekday given to
  the model was computed from raw UTC time instead of the guild's
  configured timezone, so once UTC rolled to the next calendar date the
  model was told the wrong "today." Reference time is now localized to
  `timezone_name` before being used anywhere in parsing, including the
  `dateparser` fallback path and naive (offset-less) `due_at_iso` values
  returned by the model.

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
