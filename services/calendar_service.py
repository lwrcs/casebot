import json
import logging

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from db import database as db
from db.models import UserContext
from utils import now_in

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Per-user service cache: {discord_id: service}
_service_cache: dict = {}


def get_calendar_service(conn, user_ctx: UserContext):
    discord_id = user_ctx.discord_id
    if discord_id in _service_cache:
        return _service_cache[discord_id]

    token_json = db.load_google_token(conn, discord_id)
    if not token_json:
        raise RuntimeError(f"No Google token for user {discord_id}. They need to /register first.")

    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            db.store_google_token(conn, discord_id, creds.to_json())
        else:
            raise RuntimeError(f"Google credentials invalid for {discord_id} and cannot be refreshed.")

    svc = build("calendar", "v3", credentials=creds)
    _service_cache[discord_id] = svc
    return svc


def invalidate_service_cache(discord_id: str):
    _service_cache.pop(discord_id, None)


def sync_user_calendars(conn, user_ctx: UserContext) -> tuple[list, str | None]:
    """Fetch user's calendar list from Google and persist to DB.
    Returns (calendars, detected_timezone) where detected_timezone comes from
    the primary calendar's timeZone field, or None if unavailable."""
    svc = get_calendar_service(conn, user_ctx)
    result = svc.calendarList().list().execute()
    items = result.get("items", [])
    calendars = []
    detected_timezone = None
    for item in items:
        if item.get("deleted"):
            continue
        is_primary = item.get("primary", False)
        if is_primary and item.get("timeZone"):
            detected_timezone = item["timeZone"]
        calendars.append({
            "gcal_id": item["id"],
            "name": item.get("summary", item["id"]),
            "color": item.get("colorId"),
            "is_default": is_primary,
        })
    db.upsert_user_calendars(conn, user_ctx.discord_id, calendars)
    logger.info(f"Synced {len(calendars)} calendars for {user_ctx.discord_id} (tz={detected_timezone})")
    return calendars, detected_timezone


def _resolve_calendar_id(user_ctx: UserContext, calendar_id: str | None) -> str:
    """Resolve a calendar_id param (gcal ID or partial name match) to a gcal ID."""
    if not calendar_id:
        return user_ctx.calendar_id
    # Exact match first
    for cal in user_ctx.calendars:
        if cal.gcal_id == calendar_id:
            return cal.gcal_id
    # Case-insensitive name match
    lower = calendar_id.lower()
    for cal in user_ctx.calendars:
        if lower in cal.name.lower():
            return cal.gcal_id
    # Fall back to whatever was passed (let Google reject it if invalid)
    return calendar_id


def list_events(conn, user_ctx: UserContext, time_min: str, time_max: str,
                max_results: int = 20, calendar_id: str | None = None) -> list[dict]:
    svc = get_calendar_service(conn, user_ctx)
    time_min_tz = _ensure_tz(time_min, user_ctx)
    time_max_tz = _ensure_tz(time_max, user_ctx)

    if calendar_id:
        cal_ids = [_resolve_calendar_id(user_ctx, calendar_id)]
    else:
        cal_ids = [c.gcal_id for c in user_ctx.calendars if c.whitelisted]
        if not cal_ids:
            return []

    all_events = []
    for cal_id in cal_ids:
        try:
            result = svc.events().list(
                calendarId=cal_id,
                timeMin=time_min_tz,
                timeMax=time_max_tz,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            all_events.extend(result.get("items", []))
        except HttpError:
            continue

    def _sort_key(e):
        start = e.get("start", {})
        return start.get("dateTime") or start.get("date") or ""

    all_events.sort(key=_sort_key)
    return all_events


def create_event(conn, user_ctx: UserContext, title: str, start_time: str, end_time: str,
                 description: str | None = None, calendar_id: str | None = None) -> dict:
    svc = get_calendar_service(conn, user_ctx)
    cal_id = _resolve_calendar_id(user_ctx, calendar_id)
    body = {
        "summary": title,
        "start": {"dateTime": _ensure_tz(start_time, user_ctx)},
        "end": {"dateTime": _ensure_tz(end_time, user_ctx)},
    }
    if description:
        body["description"] = description
    event = svc.events().insert(calendarId=cal_id, body=body).execute()
    event["_calendar_id"] = cal_id  # carry through so handler can store it
    return event


def _spans_multiple_days(start_str: str, end_str: str) -> bool:
    from datetime import datetime
    try:
        s = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        return s.date() != e.date()
    except Exception:
        return False


def check_availability(conn, user_ctx: UserContext, start_time: str, end_time: str) -> tuple[bool, list[dict], list[dict]]:
    """Check availability across ALL user calendars.
    Returns (no_hard_conflicts, hard_conflicts, soft_conflicts).
    Hard conflicts are timed events that overlap — they block scheduling.
    Soft conflicts are all-day or multi-day events — they warn but don't block.
    """
    svc = get_calendar_service(conn, user_ctx)
    cal_ids = [c.gcal_id for c in user_ctx.calendars if c.whitelisted]
    if not cal_ids:
        return True, [], []
    start_tz = _ensure_tz(start_time, user_ctx)
    end_tz = _ensure_tz(end_time, user_ctx)

    hard_conflicts = []
    soft_conflicts = []

    for cal_id in cal_ids:
        try:
            result = svc.events().list(
                calendarId=cal_id,
                timeMin=start_tz,
                timeMax=end_tz,
                singleEvents=True,
            ).execute()
        except HttpError:
            continue
        for event in result.get("items", []):
            if event.get("status") == "cancelled":
                continue
            start = event.get("start", {})
            end = event.get("end", {})
            entry = {"summary": event.get("summary") or "(no title)", "start": None, "end": None}
            if "date" in start:
                # All-day event — soft conflict
                entry["start"] = start["date"]
                entry["end"] = end.get("date")
                soft_conflicts.append(entry)
            else:
                dt_start = start.get("dateTime", "")
                dt_end = end.get("dateTime", "")
                entry["start"] = dt_start
                entry["end"] = dt_end
                if _spans_multiple_days(dt_start, dt_end):
                    # Multi-day timed event — soft conflict
                    soft_conflicts.append(entry)
                else:
                    hard_conflicts.append(entry)

    return len(hard_conflicts) == 0, hard_conflicts, soft_conflicts


def sync_changes(conn, user_ctx: UserContext) -> dict:
    """Incremental sync using Google's syncToken. Returns lists of added/updated/deleted gcal IDs."""
    svc = get_calendar_service(conn, user_ctx)
    discord_id = user_ctx.discord_id
    sync_token = db.get_sync_token(conn, discord_id)
    added, updated, deleted = [], [], []

    params = {
        "calendarId": user_ctx.calendar_id,
        "singleEvents": True,
    }
    if sync_token:
        params["syncToken"] = sync_token
    else:
        params["timeMin"] = now_in(user_ctx.timezone).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    try:
        page_token = None
        while True:
            if page_token:
                params["pageToken"] = page_token
            result = svc.events().list(**params).execute()

            for item in result.get("items", []):
                gcal_id = item["id"]
                if item.get("status") == "cancelled":
                    deleted.append(gcal_id)
                else:
                    existing = db.get_event_by_gcal_id(conn, discord_id, gcal_id)
                    if existing:
                        updated.append(gcal_id)
                        db.upsert_event_from_gcal(conn, discord_id, item)
                    else:
                        added.append(gcal_id)
                        db.upsert_event_from_gcal(conn, discord_id, item)

            page_token = result.get("nextPageToken")
            if not page_token:
                new_sync_token = result.get("nextSyncToken")
                if new_sync_token:
                    db.set_sync_token(conn, discord_id, new_sync_token)
                break

    except HttpError as e:
        if e.resp.status == 410:
            logger.warning(f"Sync token expired for {discord_id}, clearing for full resync")
            db.clear_sync_token(conn, discord_id)
        else:
            raise

    return {"added": added, "updated": updated, "deleted": deleted}


def _ensure_tz(dt_str: str, user_ctx: UserContext) -> str:
    from utils import local_offset_str_for
    if not dt_str:
        return dt_str
    offset = local_offset_str_for(user_ctx.timezone)
    if "T" not in dt_str:
        return dt_str + f"T00:00:00{offset}"
    if dt_str.endswith("Z") or "+" in dt_str[10:] or (dt_str.count("-") > 2):
        return dt_str
    return dt_str + offset
