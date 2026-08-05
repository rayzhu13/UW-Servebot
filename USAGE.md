# Using ServeBot

ServeBot turns a plain-English message into tracked tasks with due-date
reminders. `@mention` it and describe what you need — create tasks, mark
them done, or change something about an existing one — and it figures out
the rest.

Anyone in the server can create, close, or edit tasks. A handful of
server-wide settings (below) require Manage Server permission.

## Creating tasks

`@ServeBot` it with who owns what and when it's due:

> @ServeBot alice needs to get the slides finalized by friday at 3pm, bob
> has to send out the invoices by end of day tomorrow, and carol should
> review PR 42 sometime before next monday morning

One message can describe several tasks at once — each gets parsed out
separately.

**Custom reminder timing.** By default you'll be reminded 30 minutes before
something is due (or whatever the server has set as its default — see
below). Say when you want the heads-up instead:

> @ServeBot carol should review PR 42 by monday morning, remind her a day
> ahead

**Posting to a specific channel.** Notifications normally go to whichever
channel the task was created in. Send them somewhere else instead by
naming the channel in the same message:

> @ServeBot alice needs to finalize slides by friday 3pm, post this in
> #updates

If the channel you name doesn't exist or can't be matched, ServeBot falls
back to the server's configured reminder channel (if one is set), then to
the channel you sent the message in.

## Marking a task done

`@mention` the bot and say what's finished — no need to remember a task ID:

> @ServeBot the slides task is done
> @ServeBot alice finished the invoices

ServeBot matches your message against currently open tasks. If several
tasks look plausible, it'll only close the one it's confident about; ask
more specifically (mention the task or person) if it doesn't find it.

## Editing a task

`@mention` the bot to change something about a task that's already been
created — due date, who it's assigned to, its description, or its
reminder timing:

> @ServeBot push alice's slides task to monday morning instead
> @ServeBot reassign the invoices task to bob
> @ServeBot remind carol 2 hours before instead of a day ahead

If a requested change can't be resolved (e.g. an unparseable date, or the
new assignee can't be matched to anyone in the server), that edit is
flagged and skipped rather than partially applied — you'll see exactly
what happened in the confirmation step.

## Confirming

Before anything is created, closed, or changed, ServeBot posts a summary
and reacts with ✅ and ❌ on its own message. It shows, per task:

- **Assignee** and **Due** date/time
- **Channel** — which channel the notifications will actually post to
- **Reminder** — the exact time the reminder will fire, and how far ahead
  of due that is

Closes show as `✅ Mark done: #id — description`. Edits show as
`✏️ Edit #id — description` with a list of what's changing.

React **✅** to commit everything shown, or **❌** to discard it and try
again. Only the person who sent the original message can react to their
own confirmation — a second person's reaction is ignored, so nothing gets
committed without the requester actually reviewing it.

Anything flagged with ⚠️ ("will be skipped") in the confirmation didn't
resolve cleanly and won't be committed even if you react ✅ — everything
else in the same batch still goes through.

## What you'll be pinged with

- **Reminder** (before due): `@you reminder: {description} — due <date/time>`
- **Due now**: `📌 @you **due now:** {description}`

Both always ping the assignee directly.

## Checking on tasks

`/servebot list` — lists open (not yet done/cancelled) tasks in the
current channel.

## Server settings (require Manage Server)

- `/servebot set-reminder-channel [channel]` — send all future task
  notifications to a specific channel by default, overriding the "channel
  the task was created in" fallback. Run it with no channel to clear the
  override.
- `/servebot set-default-reminder <minutes>` — how many minutes before due
  a task gets reminded if nothing in the message says otherwise (30 by
  default).
- `/servebot set-timezone <IANA name>` — e.g. `America/New_York`,
  `Europe/London`. Used to resolve relative dates like "friday at 3pm" or
  "end of day tomorrow" for this server.
- `/servebot cancel-task <task_id>` — cancel a task outright (distinct
  from marking it done — cancelled tasks don't show up as completed,
  they're just removed from tracking). Get the id from `/servebot list`.

## Notes

- A task always gets exactly two notifications: one reminder before due,
  and one right at the due time — you can't disable the due-time one, only
  change how far ahead the reminder fires.
- Marking a task done keeps its record (just marked complete) rather than
  deleting it.
- If ServeBot doesn't find anything to create, close, or edit in your
  message, it'll say so — try rephrasing with a clearer who/what/when, or
  a clearer reference to the task you mean.
