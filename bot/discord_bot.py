"""
Discord client. Implements plan §3 (trigger/permissions), §4 Step D
(confirmation before any DB write), §7 (delivery), and §8.1 (MVP polling
scheduler).

Design choices worth flagging (see plan §12 open questions):
- Ambiguous/unresolved assignees do NOT block the whole batch — that task
  is flagged in the confirmation embed and skipped on commit, the rest
  go through. This answers open question #1 in favor of "partial commit."
- A task always needs one (primary) assignee, but may optionally be
  co-assigned to a second person. An unresolved 2nd assignee on a *new*
  task does NOT block that task (it's just created with one assignee,
  flagged in the embed) — but an *edit* that explicitly asks to add/change
  a 2nd assignee and fails to resolve is skipped entirely, per the
  all-or-nothing edit rule below.
- Only the member who sent the original message can confirm/reject their
  own pending batch, to avoid someone else accidentally confirming a batch
  they didn't review.
- Per-task reminder offsets (plan §6, listed as stretch in §10) are wired
  up here already: the parser extracts them for free, so honoring them
  when present (falling back to guild defaults otherwise) cost nothing
  extra. Everything else in §10's stretch list is still unbuilt.
- Task creation, closing, and editing are open to any server member, not
  gated by admin/Manage-Server permission — anyone can create a task, mark
  any task done, or edit any task. (Originally admin-only; opened up
  deliberately. §3's admin permission check no longer applies to
  on_message.)
- Edits are all-or-nothing per task: if a message asks to change a task's
  due date AND that new date can't be parsed, the whole edit is skipped
  rather than applying the parts that did resolve (e.g. a description
  change). Mirrors the "flag and skip" treatment new tasks and closes
  already get, for the same reason — no partial state a sender didn't
  actually confirm.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import config
from db import db
from parsing.parser import ParseError, ResolvedEdit, ResolvedTask, parse_and_normalize

log = logging.getLogger("servebot")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!servebot-unused-", intents=intents)


# ---------------------------------------------------------------------------
# Pending confirmation state (in-memory; a restart drops unconfirmed batches,
# which is the right failure mode — better to re-ask than silently commit
# something the admin never actually saw confirmed).
# ---------------------------------------------------------------------------
@dataclass
class PendingBatch:
    guild_id: int
    channel_id: int
    requested_by: int
    resolved_tasks: List[ResolvedTask]
    close_task_ids: List[int] = field(default_factory=list)
    edits: List[ResolvedEdit] = field(default_factory=list)


_pending: Dict[int, PendingBatch] = {}  # confirmation message id -> batch

CONFIRM_EMOJI = "\u2705"  # ✅
REJECT_EMOJI = "\u274c"  # ❌


# ---------------------------------------------------------------------------
# Mention trigger (plan §3, §4)
# ---------------------------------------------------------------------------
def _resolve_mentions_to_names(message: discord.Message) -> str:
    """
    Discord encodes @mentions in message.content as raw IDs like <@123456789>,
    not as visible text. Left as-is, the LLM (and our name-based assignee
    resolution) has no way to know that's a person's name. Replace every
    mention with the member's display name — except the bot's own mention,
    which is just stripped since it's the trigger, not a task subject.
    """
    text = message.content
    for member in message.mentions:
        if member.id == bot.user.id:
            continue
        for mention_form in (f"<@{member.id}>", f"<@!{member.id}>"):
            text = text.replace(mention_form, member.display_name)
    for role in message.role_mentions:
        text = text.replace(f"<@&{role.id}>", role.name)
    for channel in message.channel_mentions:
        text = text.replace(f"<#{channel.id}>", f"#{channel.name}")
    for mention_form in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
        text = text.replace(mention_form, "")
    return text.strip()


def _member_display_name(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    return member.display_name if member else f"<@{user_id}>"


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    if bot.user not in message.mentions:
        return

    raw_text = _resolve_mentions_to_names(message)

    if not raw_text:
        await message.reply(
            "Mention me along with a description of the tasks — who owns "
            "what, and when it's due — or tell me when a task is done or "
            "needs to change.",
            mention_author=True,
        )
        return

    closable = await db.list_open_tasks(message.guild.id)
    open_tasks_for_prompt = [
        {
            "id": t["id"],
            "description": t["description"],
            "assignee_display": _member_display_name(message.guild, t["assignee_id"]),
            "assignee_2_display": (
                _member_display_name(message.guild, t["assignee_id_2"]) if t["assignee_id_2"] else None
            ),
            "due_at": t["due_at"].isoformat(),
        }
        for t in closable
    ]

    settings = await db.get_guild_settings(message.guild.id)

    async with message.channel.typing():
        try:
            result = parse_and_normalize(
                raw_text,
                members=message.guild.members,
                open_tasks=open_tasks_for_prompt,
                channels=message.guild.text_channels,
                reference_time=dt.datetime.now(dt.timezone.utc),
                timezone_name=settings.timezone_name,
            )
        except ParseError as e:
            log.warning("Parse error for guild %s: %s", message.guild.id, e)
            await message.reply(
                "I couldn't make sense of that — could you rephrase it? "
                "(Who owns what and when it's due, which task is done, or "
                "what to change about one.)",
                mention_author=True,
            )
            return

    if not result.new_tasks and not result.close_task_ids and not result.edits:
        await message.reply(
            "I didn't find any tasks to create, close, or edit in that message.", mention_author=True
        )
        return

    closable_by_id = {t["id"]: t for t in closable}
    embed = _build_confirmation_embed(
        result.new_tasks, result.close_task_ids, result.edits, closable_by_id, settings, message.channel.id
    )
    confirmation_msg = await message.reply(embed=embed, mention_author=True)
    await confirmation_msg.add_reaction(CONFIRM_EMOJI)
    await confirmation_msg.add_reaction(REJECT_EMOJI)

    _pending[confirmation_msg.id] = PendingBatch(
        guild_id=message.guild.id,
        channel_id=message.channel.id,
        requested_by=message.author.id,
        resolved_tasks=result.new_tasks,
        close_task_ids=result.close_task_ids,
        edits=result.edits,
    )


def _build_confirmation_embed(
    resolved: List[ResolvedTask],
    close_task_ids: List[int],
    edits: List[ResolvedEdit],
    closable_by_id: Dict[int, dict],
    settings: db.GuildSettings,
    origin_channel_id: int,
) -> discord.Embed:
    if close_task_ids:
        title = "Here's what I parsed — confirm to close task"
        description = "React ✅ to close task, ❌ to discard."
    else:
        title = "Here's what I parsed — confirm to create task"
        description = "React ✅ to create task, ❌ to discard."
    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.blurple(),
    )
    for i, task in enumerate(resolved, start=1):
        if task.assignee_id is None:
            who = "unresolved assignee"
            if task.ambiguous_assignee_candidates:
                who += f" (matched: {', '.join(task.ambiguous_assignee_candidates)})"
            else:
                who += f" (\"{task.raw.assignee_mention}\" not found)"
        else:
            who = task.assignee_display or task.raw.assignee_mention

        if task.raw.assignee_2_mention:
            if task.assignee_id_2 is not None:
                who += f", {task.assignee_2_display or task.raw.assignee_2_mention}"
            else:
                unresolved_2 = f"\"{task.raw.assignee_2_mention}\" not found"
                if task.ambiguous_assignee_2_candidates:
                    unresolved_2 = f"matched: {', '.join(task.ambiguous_assignee_2_candidates)}"
                who += f" (2nd assignee unresolved — {unresolved_2})"

        if task.due_at is None:
            when = "no due time recognized"
        else:
            when = f"<t:{int(task.due_at.timestamp())}:F> (<t:{int(task.due_at.timestamp())}:R>)"

        effective_channel_id = task.channel_id or settings.reminder_channel_id or origin_channel_id
        channel_line = f"<#{effective_channel_id}>"
        if task.raw.channel_mention and task.channel_id is None:
            channel_line += f' ("{task.raw.channel_mention}" not found, using this instead)'

        reminder_minutes = task.raw.reminder_offset_minutes
        if reminder_minutes is None:
            reminder_minutes = settings.default_reminder_minutes
        if task.due_at is not None:
            reminder_at = task.due_at - dt.timedelta(minutes=reminder_minutes)
            reminder_ts = int(reminder_at.timestamp())
            reminder_line = f"<t:{reminder_ts}:F> (<t:{reminder_ts}:R>) — {reminder_minutes} min before due"
        else:
            reminder_line = "—"

        flag = "" if (task.assignee_id is not None and task.due_at is not None) else " — will be skipped"
        embed.add_field(
            name=f"{i}. {task.raw.description}{flag}",
            value=(
                f"**Assignee(s):** {who}\n**Due:** {when}\n"
                f"**Channel:** {channel_line}\n**Reminder:** {reminder_line}"
            ),
            inline=False,
        )

    for task_id in close_task_ids:
        t = closable_by_id.get(task_id)
        description = t["description"] if t else f"task #{task_id}"
        embed.add_field(
            name=f"Mark done: #{task_id}",
            value=description,
            inline=False,
        )

    for edit in edits:
        orig = closable_by_id.get(edit.task_id)
        orig_desc = orig["description"] if orig else f"task #{edit.task_id}"
        changes = []
        if edit.new_description:
            changes.append(f"description → {edit.new_description}")
        if edit.due_change_requested:
            if edit.new_due_at is not None:
                changes.append(f"due → <t:{int(edit.new_due_at.timestamp())}:F>")
            else:
                changes.append("due → couldn't parse new due time")
        if edit.assignee_change_requested:
            if edit.new_assignee_id is not None:
                changes.append(f"assignee → {edit.new_assignee_display}")
            else:
                who = "unresolved assignee"
                if edit.ambiguous_assignee_candidates:
                    who += f" (matched: {', '.join(edit.ambiguous_assignee_candidates)})"
                changes.append(f"assignee → {who}")
        if edit.assignee_2_change_requested:
            if edit.new_assignee_id_2 is not None:
                changes.append(f"2nd assignee → {edit.new_assignee_2_display}")
            else:
                who_2 = "unresolved 2nd assignee"
                if edit.ambiguous_assignee_2_candidates:
                    who_2 += f" (matched: {', '.join(edit.ambiguous_assignee_2_candidates)})"
                changes.append(f"2nd assignee → {who_2}")
        if edit.new_reminder_offset_minutes is not None:
            changes.append(f"reminder → {edit.new_reminder_offset_minutes} min before due")
        flag = "" if edit.is_valid else " — will be skipped"
        embed.add_field(
            name=f"Edit #{edit.task_id}: {orig_desc}{flag}",
            value="\n".join(changes) or "(no changes requested)",
            inline=False,
        )
    return embed


# ---------------------------------------------------------------------------
# Confirmation reactions (plan §4 Step D — the "single most important
# safety step" per the plan; nothing above this point ever touches the DB)
# ---------------------------------------------------------------------------
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    batch = _pending.get(payload.message_id)
    if batch is None:
        return
    if payload.user_id != batch.requested_by:
        return  # only the person who asked can confirm/reject their own batch

    emoji = str(payload.emoji)
    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return
    message = await channel.fetch_message(payload.message_id)

    if emoji == CONFIRM_EMOJI:
        committed, skipped, closed, edited = await _commit_batch(batch)
        del _pending[payload.message_id]
        parts = []
        if committed:
            parts.append(f"Scheduled {committed} task(s).")
        if closed:
            parts.append(f"Closed {closed} task(s).")
        if edited:
            parts.append(f"Edited {edited} task(s).")
        if skipped:
            parts.append(f"Skipped {skipped} unresolved task(s) — resend those separately.")
        if not parts:
            parts.append("Nothing to commit.")
        await message.reply(" ".join(parts))
    elif emoji == REJECT_EMOJI:
        del _pending[payload.message_id]
        await message.reply("Discarded — resend or edit your message to try again.")


async def _commit_batch(batch: PendingBatch) -> tuple[int, int, int, int]:
    settings = await db.get_guild_settings(batch.guild_id)
    committed = 0
    skipped = 0
    for task in batch.resolved_tasks:
        if task.assignee_id is None or task.due_at is None:
            skipped += 1
            continue
        channel_id = task.channel_id or settings.reminder_channel_id or batch.channel_id
        reminder_minutes = task.raw.reminder_offset_minutes
        if reminder_minutes is None:
            reminder_minutes = settings.default_reminder_minutes
        offsets = sorted({reminder_minutes, 0}, reverse=True)
        new_task = db.NewTask(
            guild_id=batch.guild_id,
            channel_id=channel_id,
            created_by=batch.requested_by,
            assignee_id=task.assignee_id,
            description=task.raw.description,
            due_at=task.due_at,
            offsets_minutes=offsets,
            assignee_id_2=task.assignee_id_2,
        )
        await db.create_task_with_reminders(new_task)
        committed += 1

    closed = 0
    for task_id in batch.close_task_ids:
        if await db.complete_task(task_id, batch.guild_id):
            closed += 1

    edited = 0
    for edit in batch.edits:
        if not edit.is_valid:
            skipped += 1
            continue
        ok = await db.update_task(
            edit.task_id,
            batch.guild_id,
            description=edit.new_description,
            due_at=edit.new_due_at,
            assignee_id=edit.new_assignee_id,
            assignee_id_2=edit.new_assignee_id_2,
        )
        if not ok:
            skipped += 1
            continue
        if edit.due_change_requested or edit.new_reminder_offset_minutes is not None:
            updated = await db.get_task(edit.task_id, batch.guild_id)
            if updated is not None:
                reminder_minutes = edit.new_reminder_offset_minutes
                if reminder_minutes is None:
                    existing_offsets = await db.get_active_reminder_offsets_minutes(edit.task_id)
                    non_zero = [o for o in existing_offsets if o != 0]
                    reminder_minutes = non_zero[0] if non_zero else settings.default_reminder_minutes
                offsets = sorted({reminder_minutes, 0}, reverse=True)
                await db.replace_reminders(edit.task_id, updated["due_at"], offsets)
        edited += 1

    return committed, skipped, closed, edited


# ---------------------------------------------------------------------------
# Scheduler (plan §8, MVP polling approach)
# ---------------------------------------------------------------------------
@tasks.loop(seconds=config.poll_interval_seconds)
async def reminder_loop():
    try:
        due = await db.get_due_reminders()
    except Exception:
        log.exception("Failed to fetch due reminders")
        return

    for reminder in due:
        channel = bot.get_channel(reminder.channel_id)
        if channel is None:
            log.warning("Channel %s not found for reminder %s; marking sent to avoid retry loop",
                        reminder.channel_id, reminder.reminder_id)
            await db.mark_reminder_sent(reminder.reminder_id)
            continue
        due_ts = int(reminder.due_at.timestamp())
        mentions = f"<@{reminder.assignee_id}>"
        if reminder.assignee_id_2:
            mentions += f" <@{reminder.assignee_id_2}>"
        if reminder.label == "at_due":
            text = f"{mentions} **due now:** {reminder.description}"
        else:
            text = (
                f"{mentions} reminder: {reminder.description} — "
                f"due <t:{due_ts}:F> (<t:{due_ts}:R>)"
            )
        try:
            await channel.send(text)
        except discord.HTTPException:
            log.exception("Failed to send reminder %s", reminder.reminder_id)
            continue
        await db.mark_reminder_sent(reminder.reminder_id)


@reminder_loop.before_loop
async def before_reminder_loop():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------
servebot_group = app_commands.Group(name="servebot", description="ServeBot task management")


@servebot_group.command(name="help", description="Show how to use ServeBot")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="How to use ServeBot 🤖",
        description="Just @mention the bot and tell it what you need in plain English — no commands to memorize.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Create tasks",
        value=(
            "@ServeBot alice needs to finalize the slides by friday at 3pm, bob has to "
            "send the invoices by end of day tomorrow\n\n"
            "List several tasks in one message. A task can be co-assigned to up to 2 "
            "people:\n"
            "@ServeBot alice and bob need to review the deck by friday\n\n"
            "Want a different reminder timing or a specific channel? Just say so:\n"
            "@ServeBot carol should review PR 42 by monday, remind her a day ahead\n"
            "@ServeBot alice needs the slides by friday 3pm, post this in #updates"
        ),
        inline=False,
    )
    embed.add_field(
        name="Mark something done",
        value="@ServeBot the slides task is done",
        inline=False,
    )
    embed.add_field(
        name="Change a task",
        value=(
            "@ServeBot push alice's slides task to monday instead\n"
            "@ServeBot reassign the invoices task to bob"
        ),
        inline=False,
    )
    embed.add_field(
        name="Confirming",
        value=(
            "ServeBot always shows what it understood and reacts ✅ / ❌ on its own "
            "message — nothing happens until you react ✅. Only you can confirm your "
            "own request."
        ),
        inline=False,
    )
    embed.add_field(
        name="Checking on tasks",
        value="/servebot list — see open tasks in this channel",
        inline=False,
    )
    embed.add_field(
        name="Admin settings",
        value=(
            "/servebot set-timezone\n"
            "/servebot set-reminder-channel\n"
            "/servebot set-default-reminder\n"
            "/servebot cancel-task"
        ),
        inline=False,
    )
    embed.set_footer(text="Tip: if ServeBot didn't understand your message, try rephrasing with a clearer who/what/when.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@servebot_group.command(name="set-reminder-channel", description="Set the channel reminders are posted to (omit channel to clear and use each task's origin channel)")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_reminder_channel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    await db.set_reminder_channel(interaction.guild_id, channel.id if channel else None)
    if channel is not None:
        await interaction.response.send_message(f"Reminder channel set to {channel.mention}.", ephemeral=True)
    else:
        await interaction.response.send_message(
            "Reminder channel override cleared — new tasks will post reminders in the "
            "channel they were created in.",
            ephemeral=True,
        )


@servebot_group.command(name="set-timezone", description="Set this server's timezone for interpreting due dates (e.g. America/New_York)")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_timezone(interaction: discord.Interaction, timezone_name: str):
    import zoneinfo
    try:
        zoneinfo.ZoneInfo(timezone_name)
    except zoneinfo.ZoneInfoNotFoundError:
        await interaction.response.send_message(
            f"'{timezone_name}' isn't a recognized timezone name. Use an IANA name like "
            f"`America/New_York`, `Europe/London`, or `Asia/Tokyo`.",
            ephemeral=True,
        )
        return
    await db.set_timezone(interaction.guild_id, timezone_name)
    await interaction.response.send_message(f"Timezone set to {timezone_name}.", ephemeral=True)


@servebot_group.command(name="set-default-reminder", description="Set the default reminder time (minutes before due) used when a task doesn't specify one")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_default_reminder(interaction: discord.Interaction, minutes_before_due: int):
    if minutes_before_due < 0:
        await interaction.response.send_message("Minutes before due can't be negative.", ephemeral=True)
        return
    await db.set_default_reminder_minutes(interaction.guild_id, minutes_before_due)
    await interaction.response.send_message(
        f"Default reminder set to {minutes_before_due} minute(s) before due "
        "(a due-time notification is always sent too).",
        ephemeral=True,
    )


@servebot_group.command(name="list", description="List open tasks in this channel")
async def list_tasks(interaction: discord.Interaction):
    open_tasks = await db.list_open_tasks(interaction.guild_id, interaction.channel_id)
    if not open_tasks:
        await interaction.response.send_message("No open tasks in this channel.", ephemeral=True)
        return
    def _mentions(t: dict) -> str:
        mentions = f"<@{t['assignee_id']}>"
        if t.get("assignee_id_2"):
            mentions += f" <@{t['assignee_id_2']}>"
        return mentions

    lines = [
        f"#{t['id']} — {_mentions(t)}: {t['description']} — due <t:{int(t['due_at'].timestamp())}:R>"
        for t in open_tasks
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@servebot_group.command(name="cancel-task", description="Cancel a task by id")
@app_commands.checks.has_permissions(manage_guild=True)
async def cancel_task_cmd(interaction: discord.Interaction, task_id: int):
    ok = await db.cancel_task(task_id, interaction.guild_id)
    if ok:
        await interaction.response.send_message(f"Task #{task_id} cancelled.", ephemeral=True)
    else:
        await interaction.response.send_message(f"No pending task #{task_id} found in this server.", ephemeral=True)


bot.tree.add_command(servebot_group)


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)
    try:
        await bot.tree.sync()
    except discord.HTTPException:
        log.exception("Slash command sync failed")
    if not reminder_loop.is_running():
        reminder_loop.start()
