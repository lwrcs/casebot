import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from db import database as db
from db.models import ScheduledReminder, UserContext
from utils import now_in, now_utc, offset_str_for, to_tz

logger = logging.getLogger(__name__)
scheduler: AsyncIOScheduler | None = None


def start_scheduler():
    global scheduler
    scheduler = AsyncIOScheduler(timezone="UTC")
    # Calendar sync runs globally and iterates over all active users
    scheduler.add_job(sync_calendar_changes_all_users, IntervalTrigger(minutes=5), id="calendar_sync")
    scheduler.start()
    logger.info("Scheduler started")


def stop_scheduler():
    if scheduler:
        scheduler.shutdown(wait=False)


# ── Reminder scheduling ───────────────────────────────────────────────────────

def schedule_pre_event_reminder(discord_id: str, event_id: int, event_title: str, event_start: str, advance_minutes: int):
    if scheduler is None:
        logger.warning("Scheduler not started; cannot schedule reminder")
        return
    start_dt = _parse_dt(event_start)
    fire_time = start_dt - timedelta(minutes=advance_minutes)
    if fire_time <= datetime.now(timezone.utc):
        return

    job_id = f"reminder_{discord_id}_{event_id}"
    _safe_remove_job(job_id)
    scheduler.add_job(
        send_reminder,
        DateTrigger(run_date=fire_time),
        id=job_id,
        kwargs={"discord_id": discord_id, "event_id": event_id,
                "event_title": event_title, "event_start": event_start,
                "advance_minutes": advance_minutes},
    )

    conn = db.get_connection()
    try:
        db.insert_reminder(conn, ScheduledReminder(
            user_id=discord_id,
            event_id=event_id,
            reminder_type="pre_event",
            scheduled_for=fire_time.isoformat(),
            job_id=job_id,
        ))
    finally:
        conn.close()


def schedule_follow_up(discord_id: str, event_id: int, event_title: str, event_end: str, delay_hours: float):
    if scheduler is None:
        logger.warning("Scheduler not started; cannot schedule follow-up")
        return
    end_dt = _parse_dt(event_end)
    fire_time = end_dt + timedelta(hours=delay_hours)
    if fire_time <= datetime.now(timezone.utc):
        return

    job_id = f"followup_{discord_id}_{event_id}"
    _safe_remove_job(job_id)
    scheduler.add_job(
        send_follow_up,
        DateTrigger(run_date=fire_time),
        id=job_id,
        kwargs={"discord_id": discord_id, "event_id": event_id, "event_title": event_title},
    )

    conn = db.get_connection()
    try:
        db.insert_reminder(conn, ScheduledReminder(
            user_id=discord_id,
            event_id=event_id,
            reminder_type="follow_up",
            scheduled_for=fire_time.isoformat(),
            job_id=job_id,
        ))
    finally:
        conn.close()


def schedule_morning_briefing(discord_id: str, user_timezone: str):
    """Schedule a daily 8am briefing in the user's local timezone."""
    if scheduler is None:
        return
    job_id = f"briefing_{discord_id}"
    _safe_remove_job(job_id)
    # CronTrigger with timezone fires at 8:00 in the user's local time
    scheduler.add_job(
        morning_briefing,
        CronTrigger(hour=8, minute=0, timezone=user_timezone),
        id=job_id,
        kwargs={"discord_id": discord_id},
    )


def cancel_job(job_id: str):
    _safe_remove_job(job_id)


# ── Job implementations ───────────────────────────────────────────────────────

async def send_reminder(discord_id: str, event_id: int, event_title: str, event_start: str, advance_minutes: int):
    try:
        from services.discord_service import send_dm
        conn = db.get_connection()
        try:
            user_ctx = db.get_user(conn, discord_id)
            if not user_ctx:
                return
            behavior = user_ctx.behavior
            start_dt = to_tz(_parse_dt(event_start), user_ctx.timezone)
            local_time = start_dt.strftime("%I:%M %p").lstrip("0")
            msg = f"Heads up — \"{event_title}\" starts in {advance_minutes} min (at {local_time})."
            if behavior.get("harshness_level", 5) >= 7:
                msg += " Don't blow it off."

            sent = await send_dm(msg, discord_id)
            if not sent:
                logger.error(f"Reminder for event {event_id} ({discord_id}) not delivered")
                return

            row_id = _get_reminder_id(conn, f"reminder_{discord_id}_{event_id}")
            if row_id:
                db.mark_reminder_sent(conn, row_id)
        finally:
            conn.close()
    except Exception:
        logger.exception(f"send_reminder failed for event {event_id} user {discord_id}")


async def send_follow_up(discord_id: str, event_id: int, event_title: str):
    try:
        from services.discord_service import send_dm
        conn = db.get_connection()
        try:
            event = db.get_event_by_id(conn, discord_id, event_id)
            if event and event.status in ("completed", "cancelled"):
                return

            user_ctx = db.get_user(conn, discord_id)
            harshness = user_ctx.behavior.get("harshness_level", 5) if user_ctx else 5

            if harshness >= 8:
                msg = f"You said you'd do \"{event_title}\". Did you actually do it? Reply YES or NO."
            elif harshness >= 5:
                msg = f"Hey — did you complete \"{event_title}\"? Reply YES or NO."
            else:
                msg = f"Just checking in — did you get to \"{event_title}\"? Reply YES or NO."

            sent = await send_dm(msg, discord_id)
            if not sent:
                logger.error(f"Follow-up for event {event_id} ({discord_id}) not delivered")
                return

            row_id = _get_reminder_id(conn, f"followup_{discord_id}_{event_id}")
            if row_id:
                db.mark_reminder_sent(conn, row_id)
        finally:
            conn.close()
    except Exception:
        logger.exception(f"send_follow_up failed for event {event_id} user {discord_id}")


async def morning_briefing(discord_id: str):
    try:
        from services.discord_service import send_dm
        from services import calendar_service

        conn = db.get_connection()
        try:
            user_ctx = db.get_user(conn, discord_id)
            if not user_ctx:
                return

            now = now_in(user_ctx.timezone)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = now.replace(hour=23, minute=59, second=59, microsecond=0)

            events = calendar_service.list_events(conn, user_ctx, today_start.isoformat(), today_end.isoformat())
            cutoff = now.isoformat()
            overdue = db.get_incomplete_events_before(conn, discord_id, cutoff)

            day_str = now.strftime("%A, %B %d").replace(" 0", " ")
            lines = [f"Good morning, {user_ctx.name}! Here's your day ({day_str}):"]
            if events:
                for e in events:
                    start = e.get("start", {}).get("dateTime", e.get("start", {}).get("date", ""))
                    try:
                        dt = to_tz(_parse_dt(start), user_ctx.timezone)
                        time_str = dt.strftime("%I:%M %p").lstrip("0")
                    except Exception:
                        time_str = start
                    lines.append(f"  • {time_str} — {e.get('summary', '(no title)')}")
            else:
                lines.append("  No events scheduled today.")

            if overdue:
                lines.append("\nOverdue / incomplete from the past:")
                for e in overdue[:5]:
                    lines.append(f"  • {e.title} ({e.start_time[:10]})")

            await send_dm("\n".join(lines), discord_id)
        finally:
            conn.close()
    except Exception:
        logger.exception(f"morning_briefing failed for {discord_id}")


async def sync_calendar_changes_all_users():
    try:
        from services import calendar_service
        conn = db.get_connection()
        try:
            users = db.get_all_active_users(conn)
        finally:
            conn.close()

        for user_ctx in users:
            await _sync_one_user(user_ctx)
    except Exception:
        logger.exception("sync_calendar_changes_all_users failed")


async def _sync_one_user(user_ctx: UserContext):
    try:
        from services import calendar_service
        conn = db.get_connection()
        try:
            result = calendar_service.sync_changes(conn, user_ctx)

            for gcal_id in result["deleted"]:
                event = db.get_event_by_gcal_id(conn, user_ctx.discord_id, gcal_id)
                if event:
                    _safe_remove_job(f"reminder_{user_ctx.discord_id}_{event.id}")
                    _safe_remove_job(f"followup_{user_ctx.discord_id}_{event.id}")
                    db.delete_event_by_gcal_id(conn, user_ctx.discord_id, gcal_id)
                    logger.info(f"Removed deleted event {event.id} for {user_ctx.discord_id}")

            behavior = user_ctx.behavior
            for gcal_id in result["added"]:
                event = db.get_event_by_gcal_id(conn, user_ctx.discord_id, gcal_id)
                if event:
                    schedule_pre_event_reminder(
                        user_ctx.discord_id, event.id, event.title, event.start_time,
                        behavior.get("reminder_advance_minutes", 30),
                    )
                    if behavior.get("follow_up_incomplete", True):
                        schedule_follow_up(
                            user_ctx.discord_id, event.id, event.title, event.end_time,
                            behavior.get("follow_up_delay_hours", 2),
                        )

            if any(result.values()):
                logger.info(
                    f"📅 Sync {user_ctx.discord_id}: "
                    f"+{len(result['added'])} ~{len(result['updated'])} -{len(result['deleted'])}"
                )
        finally:
            conn.close()
    except Exception:
        logger.exception(f"Calendar sync failed for {user_ctx.discord_id}")


async def recover_unsent_reminders():
    try:
        if scheduler is None:
            logger.warning("recover_unsent_reminders called before scheduler started; skipping")
            return
        conn = db.get_connection()
        try:
            unsent = db.get_all_unsent_reminders_all_users(conn)
            now = datetime.now(timezone.utc)

            for reminder in unsent:
                fire_time = _parse_dt(reminder.scheduled_for)
                if fire_time <= now:
                    db.mark_reminder_sent(conn, reminder.id)
                    continue

                if reminder.event_id and reminder.reminder_type == "pre_event":
                    event = db.get_event_by_id(conn, reminder.user_id, reminder.event_id)
                    if event:
                        user_ctx = db.get_user(conn, reminder.user_id)
                        advance = user_ctx.behavior.get("reminder_advance_minutes", 30) if user_ctx else 30
                        job_id = reminder.job_id or f"reminder_{reminder.user_id}_{reminder.event_id}"
                        _safe_remove_job(job_id)
                        scheduler.add_job(
                            send_reminder,
                            DateTrigger(run_date=fire_time),
                            id=job_id,
                            kwargs={"discord_id": reminder.user_id, "event_id": event.id,
                                    "event_title": event.title, "event_start": event.start_time,
                                    "advance_minutes": advance},
                        )
                elif reminder.event_id and reminder.reminder_type == "follow_up":
                    event = db.get_event_by_id(conn, reminder.user_id, reminder.event_id)
                    if event:
                        job_id = reminder.job_id or f"followup_{reminder.user_id}_{reminder.event_id}"
                        _safe_remove_job(job_id)
                        scheduler.add_job(
                            send_follow_up,
                            DateTrigger(run_date=fire_time),
                            id=job_id,
                            kwargs={"discord_id": reminder.user_id, "event_id": event.id,
                                    "event_title": event.title},
                        )
        finally:
            conn.close()
        logger.info("Recovered unsent reminders")
    except Exception:
        logger.exception("recover_unsent_reminders failed")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_remove_job(job_id: str):
    if scheduler and scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def _parse_dt(dt_str: str) -> datetime:
    if not dt_str:
        return datetime.now(timezone.utc)
    dt_str = dt_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(dt_str)
    except ValueError:
        dt = datetime.fromisoformat(dt_str[:19])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _get_reminder_id(conn, job_id: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM scheduled_reminders WHERE job_id=%s", (job_id,))
        row = cur.fetchone()
    return row["id"] if row else None
