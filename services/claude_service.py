import asyncio
import json
import logging

import anthropic

from config import settings
from db import database as db
from db.models import UserContext
from tools.definitions import TOOL_DEFINITIONS, TOOL_HANDLERS
from utils import now_in, offset_str_for

logger = logging.getLogger(__name__)

_client: anthropic.Anthropic | None = None

MAX_TOOL_ITERATIONS = 5
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU = "claude-haiku-4-5-20251001"

# Pricing per million tokens — update if Anthropic changes rates
_PRICING: dict[str, dict[str, float]] = {
    MODEL_SONNET: {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    MODEL_HAIKU:  {"input": 0.80, "output":  4.00, "cache_write": 1.00, "cache_read": 0.08},
}


def _get_client() -> anthropic.Anthropic:
    global _client
    if not _client:
        _client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=0)
    return _client


async def _call_api(client, **kwargs):
    """Call the Anthropic API with async retry on 529 overload."""
    delays = [5, 15, 30]
    for attempt, delay in enumerate(delays + [None]):
        try:
            return client.messages.create(**kwargs)
        except anthropic.OverloadedError:
            if delay is None:
                raise
            logger.warning(f"API overloaded, retrying in {delay}s (attempt {attempt + 1}/{len(delays)})")
            await asyncio.sleep(delay)


def build_system_prompt(user_ctx: UserContext) -> list[dict]:
    behavior = user_ctx.behavior
    now = now_in(user_ctx.timezone)
    day_name = now.strftime("%A")
    date_str = now.strftime("%B %d, %Y").replace(" 0", " ")
    time_str = now.strftime("%I:%M %p").lstrip("0")
    tz_offset = offset_str_for(user_ctx.timezone)
    today_date = now.strftime("%Y-%m-%d")

    from datetime import timedelta
    upcoming_dates = "\n".join(
        f"  {(now + timedelta(days=i)).strftime('%A')} = {(now + timedelta(days=i)).strftime('%Y-%m-%d')}"
        for i in range(7)
    )

    style_map = {
        "gentle": "Speak warmly and encouragingly. Be supportive.",
        "balanced": "Be direct and matter-of-fact. Motivate without being harsh.",
        "harsh": (
            "Be blunt and no-nonsense. Don't sugarcoat. Hold the user accountable. "
            "If they're slacking, call it out directly."
        ),
    }
    style_instruction = style_map.get(behavior.get("motivational_style", "balanced"), "")

    dynamic_block = f"""You are CaseBot, a personal planning assistant for {user_ctx.name}. You communicate exclusively via Discord DM.

Current date/time: {day_name}, {date_str} at {time_str} ({user_ctx.timezone}, UTC{tz_offset})
Today's date: {today_date}

Behavior settings:
- Persistence level: {behavior.get('persistence_level', 5)}/10
- Harshness level: {behavior.get('harshness_level', 5)}/10
- Motivational style: {behavior.get('motivational_style', 'balanced')}
- {style_instruction}
- Reminder advance: {behavior.get('reminder_advance_minutes', 30)} minutes before events
- Follow up on incomplete tasks: {behavior.get('follow_up_incomplete', True)}

Rules:
- Always use tools to fetch real calendar data — never guess or make up event details.
- Keep responses concise. Use plain text, no markdown formatting.
- When the user says "yes" or "no" after a follow-up, use update_event_status to record it.
- When scheduling, just call create_calendar_event directly — it handles availability checking internally.
- CONFLICT HANDLING: If create_calendar_event returns "status": "conflict_needs_confirmation", reply with the specific conflicting event(s) and the proposed time, then ask: "Schedule anyway? Reply YES to confirm." When the user replies YES (or yes/yeah/sure/ok), call create_calendar_event again with the EXACT same arguments PLUS force=true. Do not ask for the event details again.
- EVENT CONFIRMATION: When create_calendar_event returns "status": "created", confirm using the confirmed_start and confirmed_end fields from the response — not the times the user originally mentioned. This ensures the user sees exactly what was written to their calendar.
- BEHAVIOR CHANGES: After successfully calling update_behavior, briefly confirm to the user what changed (e.g. "Bumped harshness from 5 to 8 — you'll feel it on the next reminder."). Don't just stay silent.
- EVENT IDs: To mark an event complete/cancelled/rescheduled, first call get_calendar_events to get the event list — each entry has an integer event_id field. Pass that integer to update_event_status. Never pass the Google Calendar string ID (the long alphanumeric gcal ID) to update_event_status.
- TIMEZONE CRITICAL: {user_ctx.name} is in {user_ctx.timezone} (UTC{tz_offset}). When creating calendar events, always include the UTC offset in the ISO8601 timestamp. Example: if they say "1:30pm", use "{today_date}T13:30:00{tz_offset}". Never use Z (UTC) for event times unless the user explicitly says UTC.
- DATE REFERENCE: Use this exact lookup table for day names — do not calculate dates yourself:
{upcoming_dates}
- CALENDARS: When creating events, use the calendar_id field to target a specific calendar. If the user doesn't specify, omit calendar_id to use the default.
- OPTION MENUS: Use present_options when the user needs to pick from discrete choices (e.g. which calendar, which action, confirm vs cancel). After calling present_options, output nothing — the interactive menu is shown automatically.
- NO CALENDAR ACCESS: If any calendar tool returns {{"error": "no_calendars_connected"}}, tell the user exactly: "No calendars are connected. Use /whitelist to enable at least one calendar." Do not call any other calendar tools."""

    whitelisted = [c for c in user_ctx.calendars if c.whitelisted]
    if whitelisted:
        cal_lines = []
        for cal in whitelisted:
            marker = " [default]" if cal.is_default else ""
            cal_lines.append(f"  - {cal.name}{marker}: {cal.gcal_id}")
        dynamic_block += "\n\nAvailable calendars:\n" + "\n".join(cal_lines)

    goals_block = f"{user_ctx.name}'s goals and projects:\n\n{user_ctx.goals or '(no goals configured — ask the user to set some with /settings goals)'}"

    return [
        {"type": "text", "text": dynamic_block},
        {"type": "text", "text": goals_block, "cache_control": {"type": "ephemeral"}},
    ]


def _append_extra_context(system: list[dict], extra_context: str | None) -> list[dict]:
    if extra_context:
        system = list(system) + [{"type": "text", "text": extra_context}]
    return system


def get_conversation_messages(conn, discord_id: str) -> list[dict]:
    turns = db.get_recent_conversation(conn, discord_id, settings.CONVERSATION_HISTORY_TURNS)
    messages = [{"role": t.role, "content": t.content} for t in turns]

    merged = []
    for msg in messages:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] += "\n" + msg["content"]
        else:
            merged.append({"role": msg["role"], "content": msg["content"]})

    while merged and merged[0]["role"] != "user":
        merged.pop(0)

    if not merged:
        return []

    last_assistant_idx = None
    for i in range(len(merged) - 1, -1, -1):
        if merged[i]["role"] == "assistant":
            last_assistant_idx = i
            break

    if last_assistant_idx is not None:
        merged[last_assistant_idx]["content"] = [
            {
                "type": "text",
                "text": merged[last_assistant_idx]["content"],
                "cache_control": {"type": "ephemeral"},
            }
        ]

    return merged


def _log_cost(discord_id: str, model: str, total_usage: dict) -> None:
    p = _PRICING.get(model, _PRICING[MODEL_SONNET])
    cost = (
        total_usage["input"]       * p["input"]       / 1_000_000
        + total_usage["output"]    * p["output"]      / 1_000_000
        + total_usage["cache_write"] * p["cache_write"] / 1_000_000
        + total_usage["cache_read"]  * p["cache_read"]  / 1_000_000
    )
    tier = "haiku" if model == MODEL_HAIKU else "sonnet"
    logger.info(
        f"💰 [{discord_id}] in={total_usage['input']} out={total_usage['output']} "
        f"cw={total_usage['cache_write']} cr={total_usage['cache_read']} "
        f"→ ${cost:.4f} ({tier})"
    )


async def run_claude_turn(
    conn,
    user_message: str,
    user_ctx: UserContext,
    extra_context: str | None = None,
    use_sonnet: bool = True,
) -> str:
    model = MODEL_SONNET if use_sonnet else MODEL_HAIKU
    db.append_conversation(conn, user_ctx.discord_id, "user", user_message)

    messages = get_conversation_messages(conn, user_ctx.discord_id)
    system = _append_extra_context(build_system_prompt(user_ctx), extra_context)
    client = _get_client()

    total_usage = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}

    for iteration in range(MAX_TOOL_ITERATIONS):
        response = await _call_api(
            client,
            model=model,
            max_tokens=2048,
            system=system,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )

        u = response.usage
        total_usage["input"]       += u.input_tokens
        total_usage["output"]      += u.output_tokens
        total_usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        total_usage["cache_read"]  += getattr(u, "cache_read_input_tokens", 0) or 0

        if response.stop_reason == "end_turn":
            text = _extract_text(response)
            db.append_conversation(conn, user_ctx.discord_id, "assistant", text)
            _log_cost(user_ctx.discord_id, model, total_usage)
            return text

        if response.stop_reason == "tool_use":
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            tool_results = []

            for block in tool_use_blocks:
                try:
                    handler = TOOL_HANDLERS.get(block.name)
                    if handler:
                        result = handler(conn, user_ctx, **block.input)
                    else:
                        result = {"error": f"Unknown tool: {block.name}"}
                except Exception as e:
                    logger.exception(f"Tool {block.name} failed")
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    result = {"error": str(e)}

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        else:
            break

    fallback = "Something went wrong processing your request. Please try again."
    db.append_conversation(conn, user_ctx.discord_id, "assistant", fallback)
    _log_cost(user_ctx.discord_id, model, total_usage)
    return fallback


def _extract_text(response) -> str:
    for block in response.content:
        if hasattr(block, "text"):
            return block.text
    return ""


async def generate_facts_backup(user_ctx: UserContext, facts: list[dict]) -> str:
    raw_lines = "\n".join(
        f"[{f['created_at']}] {f['content']}"
        + (f"  (tags: {', '.join(f['tags'])})" if f["tags"] else "")
        for f in facts
    )
    client = _get_client()
    response = await _call_api(
        client,
        model=MODEL_SONNET,
        max_tokens=4096,
        system=f"You are preparing a personal knowledge backup document for {user_ctx.name}.",
        messages=[{
            "role": "user",
            "content": (
                f"Below are raw fact entries recorded about {user_ctx.name} by a personal assistant bot. "
                "Organize and transcribe them into a clean, readable plain-text document. "
                "Group related facts under clear headings (e.g. Personal, Work, Goals, Preferences, Health). "
                "Preserve every piece of information — do not omit or collapse details. "
                "Include the date each fact was recorded next to it. "
                "Use plain text only, no markdown.\n\n"
                f"RAW FACTS ({len(facts)} entries):\n{raw_lines}"
            ),
        }],
    )
    return _extract_text(response)
