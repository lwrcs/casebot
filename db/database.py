import json
import logging
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from config import settings
from crypto.encryption import decrypt, encrypt
from db.models import CalendarInfo, ConversationTurn, Event, ScheduledReminder, UserContext

logger = logging.getLogger(__name__)

DEFAULT_BEHAVIOR = {
    "persistence_level": 5,
    "harshness_level": 5,
    "motivational_style": "balanced",
    "reminder_advance_minutes": 30,
    "follow_up_incomplete": True,
    "follow_up_delay_hours": 2,
}


# ── Connection ────────────────────────────────────────────────────────────────

def get_connection():
    conn = psycopg2.connect(settings.DATABASE_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


@contextmanager
def transaction(conn):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db():
    import os
    migrations_dir = os.path.join(os.path.dirname(__file__), "migrations")
    migration_files = sorted(f for f in os.listdir(migrations_dir) if f.endswith(".sql"))
    conn = get_connection()
    try:
        for fname in migration_files:
            with open(os.path.join(migrations_dir, fname)) as f:
                sql = f.read()
            with conn.cursor() as cur:
                cur.execute(sql)
        conn.commit()
    finally:
        conn.close()


# ── Users ─────────────────────────────────────────────────────────────────────

def _dec_user(discord_id: str, val, fallback=""):
    """Decrypt a BYTEA user field, returning fallback if null or on error."""
    if not val:
        return fallback
    try:
        return decrypt(discord_id, bytes(val))
    except Exception:
        return fallback


def _load_calendars(conn, discord_id: str) -> list[CalendarInfo]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM user_calendars WHERE user_id=%s ORDER BY is_default DESC, id", (discord_id,))
        rows = cur.fetchall()
    result = []
    for r in rows:
        try:
            name = decrypt(discord_id, bytes(r["name"]))
        except Exception:
            name = "(unknown)"
        result.append(CalendarInfo(gcal_id=r["gcal_id"], name=name, color=r["color"], is_default=r["is_default"], whitelisted=r["whitelisted"]))
    return result


def get_user(conn, discord_id: str) -> UserContext | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE discord_id = %s AND active = true", (discord_id,))
        row = cur.fetchone()
    if not row:
        return None
    behavior_raw = _dec_user(discord_id, row["behavior"], "{}")
    try:
        behavior = json.loads(behavior_raw)
    except (json.JSONDecodeError, TypeError):
        behavior = {}
    calendars = _load_calendars(conn, discord_id)
    default_cal = next((c.gcal_id for c in calendars if c.is_default), row["google_calendar_id"])
    return UserContext(
        discord_id=row["discord_id"],
        name=_dec_user(discord_id, row["name"], "there") or "there",
        timezone=row["timezone"],
        behavior={**DEFAULT_BEHAVIOR, **behavior},
        goals=_dec_user(discord_id, row["goals"], ""),
        calendar_id=default_cal,
        calendars=calendars,
    )


def create_user(conn, discord_id: str, discord_username: str | None = None) -> UserContext:
    behavior_enc = encrypt(discord_id, json.dumps(dict(DEFAULT_BEHAVIOR)))
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO users (discord_id, discord_username, behavior)
               VALUES (%s, %s, %s)
               ON CONFLICT (discord_id) DO UPDATE SET active = true, discord_username = EXCLUDED.discord_username
               RETURNING *""",
            (discord_id, discord_username, behavior_enc),
        )
        row = cur.fetchone()
    conn.commit()
    return UserContext(
        discord_id=row["discord_id"],
        name=row["name"] or "there",
        timezone=row["timezone"],
        behavior=dict(DEFAULT_BEHAVIOR),
        goals="",
        calendar_id=row["google_calendar_id"],
    )


def update_user_profile(conn, discord_id: str, **fields):
    """Update name, timezone, goals, or google_calendar_id."""
    encrypted_fields = {"name", "goals"}
    plaintext_fields = {"timezone", "google_calendar_id", "discord_username"}
    allowed = encrypted_fields | plaintext_fields
    updates = {}
    for k, v in fields.items():
        if k not in allowed or v is None:
            continue
        updates[k] = encrypt(discord_id, v) if k in encrypted_fields else v
    if not updates:
        return
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE users SET {set_clause} WHERE discord_id = %s", (*updates.values(), discord_id))
    conn.commit()


def store_google_token(conn, discord_id: str, token_json: str):
    token_enc = encrypt(discord_id, token_json)
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET google_token_enc = %s WHERE discord_id = %s", (token_enc, discord_id))
    conn.commit()


def load_google_token(conn, discord_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT google_token_enc FROM users WHERE discord_id = %s", (discord_id,))
        row = cur.fetchone()
    if not row or not row["google_token_enc"]:
        return None
    return decrypt(discord_id, bytes(row["google_token_enc"]))


def upsert_user_calendars(conn, discord_id: str, calendars: list[dict]):
    """Sync calendar list from Google. Each dict: {gcal_id, name, color, is_default}."""
    for cal in calendars:
        name_enc = encrypt(discord_id, cal["name"])
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO user_calendars (user_id, gcal_id, name, color, is_default, whitelisted)
                   VALUES (%s, %s, %s, %s, %s, false)
                   ON CONFLICT (user_id, gcal_id) DO UPDATE
                   SET name=EXCLUDED.name, color=EXCLUDED.color, is_default=EXCLUDED.is_default""",
                (discord_id, cal["gcal_id"], name_enc, cal.get("color"), cal.get("is_default", False)),
            )
    conn.commit()


def set_calendar_whitelist(conn, discord_id: str, gcal_ids: list[str]):
    """Set exactly these calendars as whitelisted; all others for this user are un-whitelisted."""
    with conn.cursor() as cur:
        cur.execute("UPDATE user_calendars SET whitelisted = false WHERE user_id = %s", (discord_id,))
        if gcal_ids:
            cur.execute(
                "UPDATE user_calendars SET whitelisted = true WHERE user_id = %s AND gcal_id = ANY(%s)",
                (discord_id, gcal_ids),
            )
    conn.commit()


def set_single_calendar_whitelist(conn, discord_id: str, gcal_id: str, whitelisted: bool):
    """Set whitelist status for exactly one calendar without touching others."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE user_calendars SET whitelisted = %s WHERE user_id = %s AND gcal_id = %s",
            (whitelisted, discord_id, gcal_id),
        )
    conn.commit()


def delete_user(conn, discord_id: str):
    """Hard delete — removes all user data across all tables (GDPR)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE discord_id = %s", (discord_id,))
    conn.commit()


def get_all_active_users(conn) -> list[UserContext]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE active = true AND google_token_enc IS NOT NULL")
        rows = cur.fetchall()
    result = []
    for row in rows:
        did = row["discord_id"]
        behavior_raw = _dec_user(did, row["behavior"], "{}")
        try:
            behavior = json.loads(behavior_raw)
        except (json.JSONDecodeError, TypeError):
            behavior = {}
        calendars = _load_calendars(conn, did)
        default_cal = next((c.gcal_id for c in calendars if c.is_default), row["google_calendar_id"])
        result.append(UserContext(
            discord_id=did,
            name=_dec_user(did, row["name"], "there") or "there",
            timezone=row["timezone"],
            behavior={**DEFAULT_BEHAVIOR, **behavior},
            goals=_dec_user(did, row["goals"], ""),
            calendar_id=default_cal,
            calendars=calendars,
        ))
    return result


# ── Behavior config ───────────────────────────────────────────────────────────

def load_behavior_config(conn, discord_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT behavior FROM users WHERE discord_id = %s", (discord_id,))
        row = cur.fetchone()
    if not row:
        return dict(DEFAULT_BEHAVIOR)
    behavior_raw = _dec_user(discord_id, row["behavior"], "{}")
    try:
        behavior = json.loads(behavior_raw)
    except (json.JSONDecodeError, TypeError):
        behavior = {}
    return {**DEFAULT_BEHAVIOR, **behavior}


def save_behavior_config(conn, discord_id: str, config: dict):
    config_enc = encrypt(discord_id, json.dumps(config))
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET behavior = %s WHERE discord_id = %s", (config_enc, discord_id))
    conn.commit()


# ── Events ────────────────────────────────────────────────────────────────────

def insert_event(conn, event: Event) -> int:
    uid = event.user_id
    title_enc = encrypt(uid, event.title)
    desc_enc = encrypt(uid, event.description) if event.description else None
    notes_enc = encrypt(uid, event.notes) if event.notes else None
    goal_enc = encrypt(uid, event.goal_connection) if event.goal_connection else None
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO events (user_id, gcal_event_id, title, description, start_time, end_time,
                                   status, goal_connection, notes)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (uid, event.gcal_event_id, title_enc, desc_enc,
             event.start_time, event.end_time, event.status,
             goal_enc, notes_enc),
        )
        row = cur.fetchone()
    conn.commit()
    return row["id"]


def get_event_by_id(conn, discord_id: str, event_id: int) -> Event | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM events WHERE id = %s AND user_id = %s", (event_id, discord_id))
        row = cur.fetchone()
    return _row_to_event(row, discord_id) if row else None


def get_event_by_gcal_id(conn, discord_id: str, gcal_id: str) -> Event | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM events WHERE gcal_event_id = %s AND user_id = %s", (gcal_id, discord_id))
        row = cur.fetchone()
    return _row_to_event(row, discord_id) if row else None


def upsert_event_from_gcal(conn, discord_id: str, gcal_event: dict) -> int:
    existing = get_event_by_gcal_id(conn, discord_id, gcal_event["id"])
    start = gcal_event.get("start", {}).get("dateTime") or gcal_event.get("start", {}).get("date", "")
    end = gcal_event.get("end", {}).get("dateTime") or gcal_event.get("end", {}).get("date", "")
    title = gcal_event.get("summary") or "(no title)"
    description = gcal_event.get("description")

    if existing:
        title_enc = encrypt(discord_id, title)
        desc_enc = encrypt(discord_id, description) if description else None
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE events SET title=%s, description=%s, start_time=%s, end_time=%s,
                   updated_at=now() WHERE gcal_event_id=%s AND user_id=%s""",
                (title_enc, desc_enc, start, end, gcal_event["id"], discord_id),
            )
        conn.commit()
        return existing.id
    else:
        event = Event(
            user_id=discord_id,
            gcal_event_id=gcal_event["id"],
            title=title,
            description=description,
            start_time=start,
            end_time=end,
        )
        return insert_event(conn, event)


def update_event_status(
    conn,
    discord_id: str,
    event_id: int,
    status: str,
    notes: str | None = None,
    new_start: str | None = None,
    new_end: str | None = None,
):
    existing = get_event_by_id(conn, discord_id, event_id)
    if not existing:
        raise ValueError(f"Event {event_id} not found for user {discord_id}")

    old_snapshot = json.dumps({"status": existing.status, "notes": existing.notes,
                                "start_time": existing.start_time, "end_time": existing.end_time})
    merged_notes = existing.notes or ""
    if notes:
        merged_notes = (merged_notes + "\n" + notes).strip()

    notes_enc = encrypt(discord_id, merged_notes) if merged_notes else None
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE events SET status=%s, notes=%s,
               start_time=COALESCE(%s, start_time), end_time=COALESCE(%s, end_time),
               updated_at=now()
               WHERE id=%s AND user_id=%s""",
            (status, notes_enc, new_start, new_end, event_id, discord_id),
        )
    conn.commit()

    new_snapshot = json.dumps({"status": status, "notes": merged_notes,
                                "start_time": new_start or existing.start_time,
                                "end_time": new_end or existing.end_time})
    append_event_history(conn, discord_id, event_id, status, old_snapshot, new_snapshot, "user")


def get_events_in_range(conn, discord_id: str, start: str, end: str) -> list[Event]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM events WHERE user_id=%s AND start_time>=%s AND start_time<=%s ORDER BY start_time",
            (discord_id, start, end),
        )
        rows = cur.fetchall()
    return [_row_to_event(r, discord_id) for r in rows]


def get_incomplete_events_before(conn, discord_id: str, cutoff: str) -> list[Event]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM events WHERE user_id=%s AND end_time<=%s AND status='scheduled' ORDER BY start_time",
            (discord_id, cutoff),
        )
        rows = cur.fetchall()
    return [_row_to_event(r, discord_id) for r in rows]


def delete_event_by_gcal_id(conn, discord_id: str, gcal_id: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM events WHERE gcal_event_id=%s AND user_id=%s", (gcal_id, discord_id)
        )
        row = cur.fetchone()
        if row:
            cur.execute("DELETE FROM events WHERE id=%s", (row["id"],))
    conn.commit()


def _row_to_event(row, discord_id: str) -> Event:
    def _dec(val):
        if val is None:
            return None
        try:
            return decrypt(discord_id, bytes(val))
        except Exception:
            return str(val)

    return Event(
        id=row["id"],
        user_id=row["user_id"],
        gcal_event_id=row["gcal_event_id"],
        title=_dec(row["title"]) or "(no title)",
        description=_dec(row["description"]),
        start_time=row["start_time"],
        end_time=row["end_time"],
        status=row["status"],
        goal_connection=_dec(row["goal_connection"]),
        notes=_dec(row["notes"]),
        created_at=str(row["created_at"]) if row["created_at"] else None,
        updated_at=str(row["updated_at"]) if row["updated_at"] else None,
    )


# ── Event history ─────────────────────────────────────────────────────────────

def append_event_history(
    conn,
    discord_id: str,
    event_id: int,
    action: str,
    old_value: str | None,
    new_value: str | None,
    actor: str = "system",
):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO event_history (user_id, event_id, action, old_value, new_value, actor) VALUES (%s, %s, %s, %s, %s, %s)",
            (discord_id, event_id, action, old_value, new_value, actor),
        )
    conn.commit()


# ── Conversation history ──────────────────────────────────────────────────────

def append_conversation(conn, discord_id: str, role: str, content: str):
    content_enc = encrypt(discord_id, content)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversation_history (user_id, role, content) VALUES (%s, %s, %s)",
            (discord_id, role, content_enc),
        )
    conn.commit()


def get_recent_conversation(conn, discord_id: str, n: int) -> list[ConversationTurn]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM (
                 SELECT * FROM conversation_history WHERE user_id=%s ORDER BY id DESC LIMIT %s
               ) sub ORDER BY id ASC""",
            (discord_id, n),
        )
        rows = cur.fetchall()
    result = []
    for r in rows:
        try:
            content = decrypt(discord_id, bytes(r["content"]))
        except Exception:
            content = str(r["content"])
        result.append(ConversationTurn(
            id=r["id"],
            user_id=r["user_id"],
            role=r["role"],
            content=content,
            timestamp=str(r["timestamp"]) if r["timestamp"] else None,
        ))
    return result


# ── Scheduled reminders ───────────────────────────────────────────────────────

def insert_reminder(conn, reminder: ScheduledReminder) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO scheduled_reminders (user_id, event_id, reminder_type, scheduled_for, job_id) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (reminder.user_id, reminder.event_id, reminder.reminder_type, reminder.scheduled_for, reminder.job_id),
        )
        row = cur.fetchone()
    conn.commit()
    return row["id"]


def mark_reminder_sent(conn, reminder_id: int):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scheduled_reminders SET sent=true, sent_at=now() WHERE id=%s",
            (reminder_id,),
        )
    conn.commit()


def get_all_unsent_reminders(conn, discord_id: str) -> list[ScheduledReminder]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM scheduled_reminders WHERE user_id=%s AND sent=false",
            (discord_id,),
        )
        rows = cur.fetchall()
    return [_row_to_reminder(r) for r in rows]


def get_all_unsent_reminders_all_users(conn) -> list[ScheduledReminder]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM scheduled_reminders WHERE sent=false")
        rows = cur.fetchall()
    return [_row_to_reminder(r) for r in rows]


def _row_to_reminder(row) -> ScheduledReminder:
    return ScheduledReminder(
        id=row["id"],
        user_id=row["user_id"],
        event_id=row["event_id"],
        reminder_type=row["reminder_type"],
        scheduled_for=str(row["scheduled_for"]),
        sent=bool(row["sent"]),
        sent_at=str(row["sent_at"]) if row["sent_at"] else None,
        job_id=row["job_id"],
    )


# ── Facts & Tags ──────────────────────────────────────────────────────────────

def insert_fact(conn, discord_id: str, content: str, source: str | None) -> int:
    content_enc = encrypt(discord_id, content)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO facts (user_id, content, source) VALUES (%s, %s, %s) RETURNING id",
            (discord_id, content_enc, source),
        )
        row = cur.fetchone()
    conn.commit()
    return row["id"]


def get_all_tags(conn, discord_id: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM tags WHERE user_id=%s ORDER BY name", (discord_id,))
        rows = cur.fetchall()
    return [r["name"] for r in rows]


def upsert_tags(conn, discord_id: str, names: list[str]) -> dict[str, int]:
    result = {}
    for name in names:
        name = name.lower().strip()
        if not name:
            continue
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tags (user_id, name) VALUES (%s, %s) ON CONFLICT (user_id, name) DO NOTHING",
                (discord_id, name),
            )
            cur.execute("SELECT id FROM tags WHERE user_id=%s AND name=%s", (discord_id, name))
            row = cur.fetchone()
            result[name] = row["id"]
    conn.commit()
    return result


def tag_fact(conn, fact_id: int, tag_ids: list[int]):
    with conn.cursor() as cur:
        for tid in tag_ids:
            cur.execute(
                "INSERT INTO fact_tags (fact_id, tag_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (fact_id, tid),
            )
    conn.commit()


def get_facts_by_tags(conn, discord_id: str, tag_names: list[str]) -> list[dict]:
    if not tag_names:
        return []
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT DISTINCT f.id, f.content, f.created_at
                FROM facts f
                JOIN fact_tags ft ON f.id = ft.fact_id
                JOIN tags t ON ft.tag_id = t.id
                WHERE t.user_id=%s AND t.name = ANY(%s)
                ORDER BY f.created_at DESC
                LIMIT 20""",
            (discord_id, tag_names),
        )
        rows = cur.fetchall()

    facts = []
    for row in rows:
        try:
            content = decrypt(discord_id, bytes(row["content"]))
        except Exception:
            content = str(row["content"])
        with conn.cursor() as cur:
            cur.execute(
                """SELECT t.name FROM tags t
                   JOIN fact_tags ft ON t.id = ft.tag_id
                   WHERE ft.fact_id=%s AND t.user_id=%s""",
                (row["id"], discord_id),
            )
            tag_rows = cur.fetchall()
        facts.append({
            "id": row["id"],
            "content": content,
            "tags": [t["name"] for t in tag_rows],
        })
    return facts


# ── Calendar sync token ───────────────────────────────────────────────────────

def get_sync_token(conn, discord_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM calendar_sync WHERE user_id=%s AND key='sync_token'",
            (discord_id,),
        )
        row = cur.fetchone()
    return row["value"] if row else None


def set_sync_token(conn, discord_id: str, token: str):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO calendar_sync (user_id, key, value) VALUES (%s, 'sync_token', %s)
               ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value""",
            (discord_id, token),
        )
    conn.commit()


def clear_sync_token(conn, discord_id: str):
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM calendar_sync WHERE user_id=%s AND key='sync_token'",
            (discord_id,),
        )
    conn.commit()
