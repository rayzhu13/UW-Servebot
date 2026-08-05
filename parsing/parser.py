"""
Implements plan §4:
  Step A — LLM extraction (raw text -> structured JSON)
  Step B — Normalization (due_at_raw -> real UTC datetime, sanity-checked
           with dateparser; assignee_mention -> guild member)
  Step C — Reminder offset resolution (MVP: server defaults only; the LLM
           still extracts any offsets mentioned in-message so this is a
           one-line change to wire up later, per plan §6/§10 stretch item)

Also extracts task-completion references and task edits: given the caller's
currently-open tasks as context, the same LLM call reports which of those
the message says are done/closed, and which of those the message asks to
change (due date, assignee, description, and/or reminder time) — so
on_message can route straight to closing or editing without a second
round trip.

This module never touches the DB and never decides to commit anything —
per plan §4 Step D, confirmation always happens one layer up, in the bot.
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import re
from dataclasses import dataclass, field
from typing import List, Optional

import dateparser
from anthropic import Anthropic

from config import config

_client: Optional[Anthropic] = None


def _client_instance() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=config.anthropic_api_key)
    return _client


SYSTEM_PROMPT_TEMPLATE = """You process a message sent to ServeBot, a Discord task-reminder bot. \
The message may describe new tasks to create, may say that one or more already-open tasks are \
finished, may ask to change something about one or more already-open tasks, or any combination \
of these. Any server member may create, close, or edit tasks.

For reference, the message was sent at: {reference_iso} ({reference_weekday}), timezone {timezone_name}. \
Resolve all relative dates ("tomorrow", "friday", "next monday", "end of day") against that moment. \
"End of day" / "EOD" means 17:00 in the given timezone unless the message says otherwise. If a \
plain day-of-week is given with no time, use 09:00 that day. "Morning" means 09:00, "afternoon" \
means 14:00, "evening" means 18:00, unless the message is more specific.

Here are the currently open tasks the sender might be referring to as finished/done/closed, or \
asking to change:
{open_tasks_listing}

Return ONLY a JSON object, no prose, no markdown code fences, with exactly these three keys:

- "new_tasks": array. Each element describes one new task to create, and must have exactly \
these keys:
  - "assignee_mention": string. The name/handle the message used to refer to the person \
(e.g. "alice", "bob"). Lowercase, no @ symbol.
  - "description": string. A short, clear restatement of the task (imperative form, \
e.g. "Finalize slides"). Do not include the due date/time in this field.
  - "due_at_raw": string. The exact phrase from the message describing when it's due \
(e.g. "friday at 3pm", "end of day tomorrow"). If genuinely no due time was given, \
use an empty string.
  - "due_at_iso": string. Your best-effort resolution of due_at_raw into a full ISO 8601 \
datetime with UTC offset (e.g. "2026-07-31T21:00:00+00:00"), computed per the rules \
above. If due_at_raw is empty, use an empty string here too.
  - "reminder_offset_minutes": integer or null. Minutes-before-due-time for a \
reminder, ONLY if the message explicitly mentions reminder timing for THIS task \
(e.g. "remind her a day ahead" -> 1440). If no reminder timing was mentioned for \
this task, use null — do not guess or invent one. A separate due-time notification \
is always sent regardless of this value, so don't use 0 to mean "no reminder."
  - "channel_mention": string or null. The channel name the message asks THIS \
task's notifications to be posted in (e.g. "#updates" or "post this in \
channel-xyz" -> "updates" or "channel-xyz", without the leading #). Use null if \
no channel was mentioned for this task.
  If the message describes zero new tasks, return an empty array.

- "close_task_ids": array of integers. The "id" of every task listed above that this message \
says is done, finished, closed, completed, or no longer needed. Only include an id if the \
message clearly refers to that specific task — do not guess, and never invent an id that \
wasn't listed above. If the message doesn't reference finishing/closing anything, use an \
empty array.

- "edits": array. Each element describes a change to ONE already-open task listed above (never \
a task being closed in the same message, and never a brand-new task). Must have exactly these \
keys:
  - "task_id": integer. Must be an id from the open-tasks list above — never invent one.
  - "new_description": string or null. Only set if the message asks to reword/replace the \
task's description; otherwise null.
  - "new_due_at_raw": string or null. The exact phrase describing the new due time \
(e.g. "push it to friday", "next monday morning"), only if the message asks to change the due \
date/time; otherwise null.
  - "new_due_at_iso": string or null. Your best-effort ISO 8601 resolution of new_due_at_raw \
per the date rules above; null if new_due_at_raw is null.
  - "new_assignee_mention": string or null. Lowercase, no @ symbol; only if the message asks \
to reassign the task to someone else; otherwise null.
  - "new_reminder_offset_minutes": integer or null. Only if the message explicitly asks to \
change the reminder timing for this task; otherwise null.
  Only include an edit for a task if the message clearly asks to change something about it — \
do not guess. If the message doesn't ask to change anything, return an empty array.

Never include any key not listed above. Never wrap any array in another object."""


class ParseError(Exception):
    pass


@dataclass
class RawExtractedTask:
    assignee_mention: str
    description: str
    due_at_raw: str
    due_at_iso: str = ""
    reminder_offset_minutes: Optional[int] = None
    channel_mention: Optional[str] = None


@dataclass
class RawExtractedEdit:
    task_id: int
    new_description: Optional[str] = None
    new_due_at_raw: Optional[str] = None
    new_due_at_iso: Optional[str] = None
    new_assignee_mention: Optional[str] = None
    new_reminder_offset_minutes: Optional[int] = None


@dataclass
class ParsedMessage:
    new_tasks: List[RawExtractedTask]
    close_task_ids: List[int]
    edits: List[RawExtractedEdit]


def extract_message(
    raw_message: str,
    reference_time: dt.datetime,
    timezone_name: str,
    open_tasks: List[dict],
) -> ParsedMessage:
    """Step A: send raw text to the Anthropic API, get back structured JSON.

    The model is given the message's reference time/weekday/timezone and asked
    to resolve due dates itself (it handles "end of day tomorrow" or "next
    monday morning" far better than a rules-based date library) — dateparser
    is used later only as a fallback if the model's ISO string is missing or
    unparseable.

    Also given the caller's currently-open tasks (id, description, assignee,
    due date) so it can report which of those the message says are done —
    one call covers both new-task extraction and completion detection.
    `open_tasks` items need "id", "description", "assignee_display", "due_at".
    """
    client = _client_instance()
    open_tasks_listing = "\n".join(
        f'- id={t["id"]}: "{t["description"]}" (assigned to {t["assignee_display"]}, due {t["due_at"]})'
        for t in open_tasks
    ) or "(none)"
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        reference_iso=reference_time.isoformat(),
        reference_weekday=reference_time.strftime("%A"),
        timezone_name=timezone_name,
        open_tasks_listing=open_tasks_listing,
    )
    response = client.messages.create(
        model=config.anthropic_model,
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": raw_message}],
    )
    text_blocks = [block.text for block in response.content if block.type == "text"]
    raw_text = "".join(text_blocks).strip()

    # Defensive: strip markdown fences if the model adds them despite instructions.
    raw_text = re.sub(r"^```(json)?|```$", "", raw_text, flags=re.MULTILINE).strip()

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ParseError(f"Model did not return valid JSON: {e}\nRaw output: {raw_text!r}")

    if not isinstance(parsed, dict):
        raise ParseError(f"Expected a JSON object, got: {type(parsed).__name__}")

    new_tasks = []
    for i, item in enumerate(parsed.get("new_tasks", [])):
        try:
            raw_offset = item.get("reminder_offset_minutes")
            channel_mention = item.get("channel_mention")
            new_tasks.append(
                RawExtractedTask(
                    assignee_mention=str(item["assignee_mention"]).strip().lower(),
                    description=str(item["description"]).strip(),
                    due_at_raw=str(item.get("due_at_raw", "")).strip(),
                    due_at_iso=str(item.get("due_at_iso", "")).strip(),
                    reminder_offset_minutes=int(raw_offset) if raw_offset is not None else None,
                    channel_mention=str(channel_mention).strip() if channel_mention else None,
                )
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ParseError(f"Malformed task at index {i}: {e}\nItem: {item!r}")

    try:
        close_task_ids = [int(x) for x in parsed.get("close_task_ids", [])]
    except (TypeError, ValueError) as e:
        raise ParseError(f"Malformed close_task_ids: {e}\nValue: {parsed.get('close_task_ids')!r}")

    edits = []
    for i, item in enumerate(parsed.get("edits", [])):
        try:
            raw_reminder = item.get("new_reminder_offset_minutes")
            new_assignee_mention = item.get("new_assignee_mention")
            edits.append(
                RawExtractedEdit(
                    task_id=int(item["task_id"]),
                    new_description=(str(item["new_description"]).strip() or None)
                    if item.get("new_description") is not None else None,
                    new_due_at_raw=item.get("new_due_at_raw") or None,
                    new_due_at_iso=item.get("new_due_at_iso") or None,
                    new_assignee_mention=str(new_assignee_mention).strip().lower()
                    if new_assignee_mention else None,
                    new_reminder_offset_minutes=int(raw_reminder) if raw_reminder is not None else None,
                )
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ParseError(f"Malformed edit at index {i}: {e}\nItem: {item!r}")

    return ParsedMessage(new_tasks=new_tasks, close_task_ids=close_task_ids, edits=edits)


@dataclass
class ResolvedTask:
    raw: RawExtractedTask
    assignee_id: Optional[int]          # None => ambiguous/unresolved
    assignee_display: Optional[str]      # best-guess display name, for the confirmation embed
    due_at: Optional[dt.datetime]        # None => could not parse a due time
    channel_id: Optional[int] = None     # None => no channel mentioned, or mentioned but not found
    ambiguous_assignee_candidates: List[str] = field(default_factory=list)


def resolve_assignee(mention: str, members: List) -> tuple[Optional[int], Optional[str], List[str]]:
    """
    Resolve a freeform name against a guild's member list.
    members: list of discord.Member (duck-typed here to keep this module import-light
    for unit testing — needs .id, .name, .display_name, .nick).
    Returns (member_id_or_None, display_name_or_None, ambiguous_candidates).
    """
    mention_lower = mention.lower().lstrip("@").strip()
    if not mention_lower:
        return None, None, []

    # 1. Exact match on username, display name, or nickname.
    exact_matches = [
        m for m in members
        if mention_lower in {
            (m.name or "").lower(),
            (m.display_name or "").lower(),
            (getattr(m, "nick", None) or "").lower(),
        }
    ]
    if len(exact_matches) == 1:
        m = exact_matches[0]
        return m.id, m.display_name, []
    if len(exact_matches) > 1:
        return None, None, [m.display_name for m in exact_matches]

    # 2. Fuzzy match as a fallback.
    name_pool = {}
    for m in members:
        for candidate in {m.name, m.display_name, getattr(m, "nick", None)}:
            if candidate:
                name_pool[candidate.lower()] = m

    close = difflib.get_close_matches(mention_lower, name_pool.keys(), n=3, cutoff=0.75)
    if len(close) == 1:
        m = name_pool[close[0]]
        return m.id, m.display_name, []
    if len(close) > 1:
        return None, None, [name_pool[c].display_name for c in close]

    return None, None, []


def resolve_channel(mention: Optional[str], channels: List) -> Optional[int]:
    """
    Resolve a freeform channel name against a guild's text channels.
    channels: list of discord.TextChannel (duck-typed — needs .id, .name).
    Returns the channel id, or None if no mention was given, or it didn't
    resolve to exactly one channel (never guess between ambiguous matches).
    """
    if not mention:
        return None
    mention_lower = mention.lower().lstrip("#").strip()
    if not mention_lower:
        return None

    exact_matches = [c for c in channels if (c.name or "").lower() == mention_lower]
    if len(exact_matches) == 1:
        return exact_matches[0].id
    if len(exact_matches) > 1:
        return None

    name_pool = {c.name.lower(): c for c in channels if c.name}
    close = difflib.get_close_matches(mention_lower, name_pool.keys(), n=3, cutoff=0.75)
    if len(close) == 1:
        return name_pool[close[0]].id

    return None


def normalize_due_at(
    due_at_raw: str,
    due_at_iso: str,
    reference_time: dt.datetime,
    timezone_name: str,
) -> Optional[dt.datetime]:
    """
    Step B: resolve a due date into a real UTC datetime.

    Primary: the model's own due_at_iso (it has full sentence context, so it
    handles "end of day tomorrow" / "next monday morning" far better than a
    rules-based parser). Fallback: dateparser against due_at_raw, per plan §4's
    "cheap insurance" framing, for the case where the model didn't produce a
    usable ISO string.
    """
    if due_at_iso:
        try:
            parsed = dt.datetime.fromisoformat(due_at_iso)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
        except ValueError:
            log_msg = f"Model returned unparseable due_at_iso {due_at_iso!r}, falling back to dateparser"
            _fallback_log(log_msg)

    if not due_at_raw:
        return None

    settings = {
        "RELATIVE_BASE": reference_time,
        "PREFER_DATES_FROM": "future",
        "TIMEZONE": timezone_name,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "TO_TIMEZONE": "UTC",
    }
    return dateparser.parse(due_at_raw, settings=settings)


def _fallback_log(message: str) -> None:
    import logging
    logging.getLogger("servebot.parsing").warning(message)


@dataclass
class ResolvedEdit:
    task_id: int
    new_description: Optional[str]
    new_due_at: Optional[dt.datetime]
    due_change_requested: bool
    new_assignee_id: Optional[int]
    new_assignee_display: Optional[str]
    assignee_change_requested: bool
    new_reminder_offset_minutes: Optional[int]
    ambiguous_assignee_candidates: List[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """False if a requested change couldn't be resolved, or nothing was
        actually requested — either way the edit should be skipped, not
        partially applied."""
        if self.due_change_requested and self.new_due_at is None:
            return False
        if self.assignee_change_requested and self.new_assignee_id is None:
            return False
        return bool(
            self.new_description
            or self.due_change_requested
            or self.assignee_change_requested
            or self.new_reminder_offset_minutes is not None
        )


@dataclass
class ParseResult:
    new_tasks: List[ResolvedTask]
    close_task_ids: List[int]
    edits: List[ResolvedEdit]


def parse_and_normalize(
    raw_message: str,
    members: List,
    open_tasks: List[dict],
    channels: Optional[List] = None,
    reference_time: Optional[dt.datetime] = None,
    timezone_name: str = "UTC",
) -> ParseResult:
    """Full pipeline: Step A extraction + Step B normalization for one message.

    `open_tasks` is the set of currently-open tasks the caller is allowed to
    close or edit (see extract_message) — close_task_ids and edits' task_ids
    are both filtered down to ids actually present in that set, as a
    defensive check against the model inventing an id it wasn't given.

    `channels` (discord.TextChannel-like, needs .id/.name) resolves each new
    task's optional channel_mention to a channel_id — the caller (on_message)
    decides the final fallback chain when this is None/unresolved.
    """
    channels = channels or []
    reference_time = reference_time or dt.datetime.now(dt.timezone.utc)
    parsed = extract_message(raw_message, reference_time, timezone_name, open_tasks)

    resolved = []
    for task in parsed.new_tasks:
        assignee_id, display_name, ambiguous = resolve_assignee(task.assignee_mention, members)
        due_at = normalize_due_at(task.due_at_raw, task.due_at_iso, reference_time, timezone_name)
        channel_id = resolve_channel(task.channel_mention, channels)
        resolved.append(
            ResolvedTask(
                raw=task,
                assignee_id=assignee_id,
                assignee_display=display_name,
                due_at=due_at,
                channel_id=channel_id,
                ambiguous_assignee_candidates=ambiguous,
            )
        )

    valid_ids = {t["id"] for t in open_tasks}
    close_task_ids = [tid for tid in parsed.close_task_ids if tid in valid_ids]

    resolved_edits = []
    for edit in parsed.edits:
        if edit.task_id not in valid_ids:
            continue
        due_change_requested = bool(edit.new_due_at_raw or edit.new_due_at_iso)
        new_due_at = None
        if due_change_requested:
            new_due_at = normalize_due_at(
                edit.new_due_at_raw or "", edit.new_due_at_iso or "", reference_time, timezone_name
            )
        assignee_change_requested = bool(edit.new_assignee_mention)
        new_assignee_id = new_assignee_display = None
        ambiguous = []
        if assignee_change_requested:
            new_assignee_id, new_assignee_display, ambiguous = resolve_assignee(
                edit.new_assignee_mention, members
            )
        resolved_edits.append(
            ResolvedEdit(
                task_id=edit.task_id,
                new_description=edit.new_description,
                new_due_at=new_due_at,
                due_change_requested=due_change_requested,
                new_assignee_id=new_assignee_id,
                new_assignee_display=new_assignee_display,
                assignee_change_requested=assignee_change_requested,
                new_reminder_offset_minutes=edit.new_reminder_offset_minutes,
                ambiguous_assignee_candidates=ambiguous,
            )
        )

    return ParseResult(new_tasks=resolved, close_task_ids=close_task_ids, edits=resolved_edits)
