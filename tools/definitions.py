import json
import logging

from db import database as db
from db.models import Event, UserContext
from services import calendar_service
from utils import now_in

logger = logging.getLogger(__name__)

_NO_CALENDAR_ERROR = {
    "error": "no_calendars_connected",
    "message": (
        "No calendars are connected. Use /whitelist to enable at least one calendar "
        "before using calendar features."
    ),
}


def _has_calendar_access(user_ctx: UserContext) -> bool:
    return any(c.whitelisted for c in user_ctx.calendars)


# Pending option menus waiting for a reaction: discord_id → {prompt, options}
_pending_option_data: dict[str, dict] = {}


def pop_pending_option_menu(discord_id: str) -> dict | None:
    return _pending_option_data.pop(discord_id, None)

TOOL_DEFINITIONS = [
    {
        "name": "get_calendar_events",
        "description": (
            "Retrieve events from Google Calendar for a given time range. Use this to answer "
            "questions about the user's upcoming schedule, check what's planned, or look for conflicts. "
            "Each event in the response includes an event_id (integer) — use that value when calling "
            "update_event_status or update_calendar_event."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "time_min": {
                    "type": "string",
                    "description": "ISO8601 datetime for start of range (e.g. '2026-05-05T00:00:00Z')",
                },
                "time_max": {
                    "type": "string",
                    "description": "ISO8601 datetime for end of range",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of events to return. Default 20.",
                },
            },
            "required": ["time_min", "time_max"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": (
            "Create a new event on Google Calendar and record it locally. "
            "Use when the user wants to schedule something."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start_time": {"type": "string", "description": "ISO8601 datetime"},
                "end_time": {"type": "string", "description": "ISO8601 datetime"},
                "description": {"type": "string", "description": "Optional event notes"},
                "goal_connection": {
                    "type": "string",
                    "description": "Which goal from goals this event serves",
                },
                "calendar_id": {
                    "type": "string",
                    "description": "Calendar to create the event on. Use the gcal_id from the available calendars list in the system prompt. Omit to use the default calendar.",
                },
                "force": {
                    "type": "boolean",
                    "description": "If true, create the event even if there is a scheduling conflict. Only set after the user has confirmed they want to proceed despite the conflict.",
                },
            },
            "required": ["title", "start_time", "end_time"],
        },
    },
    {
        "name": "update_event_status",
        "description": (
            "Mark an event as completed, incomplete, rescheduled, or cancelled. "
            "Optionally append notes. Use when the user reports on an event."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "integer", "description": "Local DB event ID — the event_id integer returned by get_calendar_events. Never use the Google Calendar string ID here."},
                "status": {
                    "type": "string",
                    "enum": ["completed", "incomplete", "cancelled", "rescheduled"],
                },
                "notes": {"type": "string", "description": "Note to append to the event"},
                "new_start_time": {
                    "type": "string",
                    "description": "Required if status is 'rescheduled'. ISO8601.",
                },
                "new_end_time": {
                    "type": "string",
                    "description": "Required if status is 'rescheduled'. ISO8601.",
                },
            },
            "required": ["event_id", "status"],
        },
    },
    {
        "name": "check_availability",
        "description": (
            "Check whether a given time slot is free on Google Calendar. "
            "Returns whether the slot is available and any conflicting events."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_time": {"type": "string", "description": "ISO8601 datetime"},
                "end_time": {"type": "string", "description": "ISO8601 datetime"},
            },
            "required": ["start_time", "end_time"],
        },
    },
    {
        "name": "update_behavior",
        "description": (
            "Modify CaseBot's behavioral settings. Use when the user explicitly asks to change "
            "how CaseBot behaves (e.g. 'be more harsh', 'remind me earlier')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "persistence_level": {"type": "integer", "minimum": 1, "maximum": 10},
                "harshness_level": {"type": "integer", "minimum": 1, "maximum": 10},
                "motivational_style": {
                    "type": "string",
                    "enum": ["gentle", "balanced", "harsh"],
                },
                "reminder_advance_minutes": {"type": "integer", "minimum": 5, "maximum": 120},
                "follow_up_incomplete": {"type": "boolean"},
                "follow_up_delay_hours": {"type": "number", "minimum": 0.5, "maximum": 24},
            },
        },
    },
    {
        "name": "present_options",
        "description": (
            "Display a numbered option menu to the user. They select by reacting with a number emoji. "
            "Use when the user needs to choose between discrete items — calendar selection, action choices, "
            "confirmation paths. After calling this tool output nothing; the menu is shown interactively."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Question or intro text shown above the options.",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The choices to present. Maximum 9.",
                },
            },
            "required": ["prompt", "options"],
        },
    },
    {
        "name": "suggest_best_use_of_time",
        "description": (
            "Analyze goals, recent completion history, and current calendar to suggest the best "
            "use of available time. Call this when the user asks 'what should I work on?' or "
            "'what's a good use of my time right now?'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "available_from": {
                    "type": "string",
                    "description": "ISO8601 start of free window (optional)",
                },
                "available_until": {
                    "type": "string",
                    "description": "ISO8601 end of free window (optional)",
                },
                "context": {
                    "type": "string",
                    "description": "User-provided context about energy level, mood, constraints",
                },
            },
        },
    },
]


# ── Handlers ──────────────────────────────────────────────────────────────────

def handle_get_calendar_events(conn, user_ctx: UserContext, time_min: str, time_max: str, max_results: int = 20) -> dict:
    if not _has_calendar_access(user_ctx):
        return _NO_CALENDAR_ERROR
    events = calendar_service.list_events(conn, user_ctx, time_min, time_max, max_results)
    simplified = []
    for e in events:
        local_id = db.upsert_event_from_gcal(conn, user_ctx.discord_id, e)
        simplified.append({
            "event_id": local_id,
            "title": e.get("summary") or "(no title)",
            "start": e.get("start", {}).get("dateTime") or e.get("start", {}).get("date"),
            "end": e.get("end", {}).get("dateTime") or e.get("end", {}).get("date"),
            "description": e.get("description"),
        })
    return {"events": simplified, "count": len(simplified)}


def handle_create_calendar_event(
    conn,
    user_ctx: UserContext,
    title: str,
    start_time: str,
    end_time: str,
    description: str | None = None,
    goal_connection: str | None = None,
    calendar_id: str | None = None,
    force: bool = False,
) -> dict:
    if not _has_calendar_access(user_ctx):
        return _NO_CALENDAR_ERROR
    soft_conflicts = []
    if not force:
        is_free, hard_conflicts, soft_conflicts = calendar_service.check_availability(conn, user_ctx, start_time, end_time)
        if not is_free:
            return {
                "status": "conflict_needs_confirmation",
                "conflicts": [{"start": c.get("start"), "end": c.get("end"), "summary": c.get("summary", "(no title)")} for c in hard_conflicts],
                "proposed": {"title": title, "start": start_time, "end": end_time},
                "instruction": "Tell the user exactly what the proposed event conflicts with, then ask if they want to schedule it anyway. If they say YES (or yes/yeah/sure), call create_calendar_event again with the same parameters plus force=true.",
            }

    try:
        gcal_event = calendar_service.create_event(conn, user_ctx, title, start_time, end_time, description, calendar_id)
        gcal_id = gcal_event.get("id")
        used_calendar_id = gcal_event.get("_calendar_id", user_ctx.calendar_id)
        if not gcal_id:
            return {"status": "error", "message": "Google Calendar returned no event ID — event may not have been created."}
    except Exception as e:
        return {"status": "error", "message": f"Could not create event on Google Calendar: {e}"}

    try:
        event = Event(
            user_id=user_ctx.discord_id,
            gcal_event_id=gcal_id,
            title=title,
            description=description,
            start_time=start_time,
            end_time=end_time,
            goal_connection=goal_connection,
        )
        local_id = db.insert_event(conn, event)
        db.append_event_history(
            conn, user_ctx.discord_id, local_id, "created", None,
            json.dumps({"title": title, "start": start_time, "end": end_time}),
            "claude",
        )
    except Exception as e:
        try:
            svc = calendar_service.get_calendar_service(conn, user_ctx)
            svc.events().delete(calendarId=used_calendar_id, eventId=gcal_id).execute()
        except Exception:
            pass
        return {"status": "error", "message": f"DB insert failed; calendar rolled back: {e}"}

    behavior = user_ctx.behavior
    from services import scheduler_service
    try:
        scheduler_service.schedule_pre_event_reminder(
            user_ctx.discord_id, local_id, title, start_time,
            behavior.get("reminder_advance_minutes", 30),
        )
        if behavior.get("follow_up_incomplete", True):
            scheduler_service.schedule_follow_up(
                user_ctx.discord_id, local_id, title, end_time,
                behavior.get("follow_up_delay_hours", 2),
            )
    except Exception as e:
        logger.warning(f"Event {local_id} created but reminder scheduling failed: {e}")

    cal_name = next((c.name for c in user_ctx.calendars if c.gcal_id == used_calendar_id), used_calendar_id)
    confirmed_start = gcal_event.get("start", {}).get("dateTime") or gcal_event.get("start", {}).get("date")
    confirmed_end = gcal_event.get("end", {}).get("dateTime") or gcal_event.get("end", {}).get("date")
    result = {
        "status": "created",
        "event_id": local_id,
        "gcal_event_id": gcal_id,
        "title": gcal_event.get("summary") or title,
        "calendar": cal_name,
        "confirmed_start": confirmed_start,
        "confirmed_end": confirmed_end,
    }
    if soft_conflicts:
        result["warnings"] = [f"overlaps with '{c['summary']}' ({c['start']} – {c['end']})" for c in soft_conflicts]
    return result


def handle_update_event_status(
    conn,
    user_ctx: UserContext,
    event_id: int,
    status: str,
    notes: str | None = None,
    new_start_time: str | None = None,
    new_end_time: str | None = None,
) -> dict:
    db.update_event_status(conn, user_ctx.discord_id, event_id, status, notes, new_start_time, new_end_time)

    if status == "rescheduled" and new_start_time and new_end_time:
        event = db.get_event_by_id(conn, user_ctx.discord_id, event_id)
        if event:
            from services import scheduler_service
            scheduler_service.cancel_job(f"reminder_{user_ctx.discord_id}_{event_id}")
            scheduler_service.cancel_job(f"followup_{user_ctx.discord_id}_{event_id}")
            behavior = user_ctx.behavior
            scheduler_service.schedule_pre_event_reminder(
                user_ctx.discord_id, event_id, event.title, new_start_time,
                behavior.get("reminder_advance_minutes", 30),
            )
            if behavior.get("follow_up_incomplete", True):
                scheduler_service.schedule_follow_up(
                    user_ctx.discord_id, event_id, event.title, new_end_time,
                    behavior.get("follow_up_delay_hours", 2),
                )

    return {"event_id": event_id, "status": status, "updated": True}


def handle_check_availability(conn, user_ctx: UserContext, start_time: str, end_time: str) -> dict:
    if not _has_calendar_access(user_ctx):
        return _NO_CALENDAR_ERROR
    is_free, hard_conflicts, soft_conflicts = calendar_service.check_availability(conn, user_ctx, start_time, end_time)
    return {"available": is_free, "conflicts": hard_conflicts, "all_day_events": soft_conflicts}


def handle_present_options(conn, user_ctx: UserContext, prompt: str, options: list) -> dict:
    clamped = options[:9]
    _pending_option_data[user_ctx.discord_id] = {"prompt": prompt, "options": clamped}
    return {"status": "option_menu_ready", "options_count": len(clamped)}


def handle_update_behavior(conn, user_ctx: UserContext, **kwargs) -> dict:
    config = db.load_behavior_config(conn, user_ctx.discord_id)
    updated_fields = {}
    for key, value in kwargs.items():
        if value is not None and key in config:
            config[key] = value
            updated_fields[key] = value
    db.save_behavior_config(conn, user_ctx.discord_id, config)
    return {"updated_fields": updated_fields, "full_config": config}


def handle_suggest_best_use_of_time(
    conn,
    user_ctx: UserContext,
    available_from: str | None = None,
    available_until: str | None = None,
    context: str | None = None,
) -> dict:
    if not _has_calendar_access(user_ctx):
        return _NO_CALENDAR_ERROR
    now = now_in(user_ctx.timezone)
    start = available_from or now.isoformat()
    end = available_until or (now.replace(hour=23, minute=59)).isoformat()

    events = calendar_service.list_events(conn, user_ctx, start, end)
    busy_slots = [
        {
            "title": e.get("summary") or "(no title)",
            "start": e.get("start", {}).get("dateTime"),
            "end": e.get("end", {}).get("dateTime"),
        }
        for e in events
    ]

    thirty_days_ago = now.replace(day=max(1, now.day - 30)).isoformat()
    past_events = db.get_events_in_range(conn, user_ctx.discord_id, thirty_days_ago, now.isoformat())
    total = len(past_events)
    completed = sum(1 for e in past_events if e.status == "completed")
    completion_rate = round(completed / total, 2) if total > 0 else None

    incomplete = [e.title for e in past_events if e.status in ("scheduled", "incomplete")][:10]

    return {
        "busy_slots_in_window": busy_slots,
        "completion_rate_30d": completion_rate,
        "incomplete_recent_tasks": incomplete,
        "context": context,
        "note": "Use the goals and this data to reason about what the user should prioritize.",
    }


TOOL_HANDLERS = {
    "get_calendar_events": handle_get_calendar_events,
    "create_calendar_event": handle_create_calendar_event,
    "update_event_status": handle_update_event_status,
    "check_availability": handle_check_availability,
    "present_options": handle_present_options,
    "update_behavior": handle_update_behavior,
    "suggest_best_use_of_time": handle_suggest_best_use_of_time,
}
