import asyncio
import logging
import threading

import anthropic
import discord
import uvicorn
from googleapiclient.errors import HttpError

from agents import analyst, listener, tag_manager, tagger
from config import settings
from db import database as db
from services import calendar_service, claude_service, discord_service, scheduler_service
from tools.definitions import pop_pending_option_menu
from web.app import app as web_app, set_discord_client
from web.oauth import make_state_token

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
)
for _noisy in ("discord", "googleapiclient", "google", "apscheduler", "httpx", "httpcore", "uvicorn.access"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def _log(symbol: str, msg: str):
    logger.info(f"{symbol} {msg}")


def _is_dev(discord_id: str) -> bool:
    return bool(settings.DEV_DISCORD_ID) and discord_id == settings.DEV_DISCORD_ID


@discord_service.client.event
async def on_ready():
    _log("🤖", f"CaseBot ready ({discord_service.client.user})")
    set_discord_client(discord_service.client)
    scheduler_service.start_scheduler()
    await scheduler_service.recover_unsent_reminders()
    # Re-schedule morning briefings for all active users
    conn = db.get_connection()
    try:
        users = db.get_all_active_users(conn)
        for u in users:
            scheduler_service.schedule_morning_briefing(u.discord_id, u.timezone)
            try:
                from services import calendar_service as cal_svc
                cal_svc.sync_user_calendars(conn, u)
            except Exception:
                logger.exception(f"Calendar list sync failed on startup for {u.discord_id}")
        if users:
            _log("📅", f"Scheduled morning briefings and synced calendars for {len(users)} user(s)")
    finally:
        conn.close()


WELCOME_MESSAGE = """\
Hey {name}! I'm **CaseBot** — a personal planning assistant that lives in your DMs and connects to your Google Calendar.

**What I can do**
• Schedule, view, and manage events on your Google Calendar using plain English
• Check for conflicts before booking anything
• Send you a reminder before events and follow up afterward to see if you actually did the thing
• Remember facts about you across conversations — your goals, preferences, habits — and use them when answering questions
• Give you a morning briefing every day with your schedule and any overdue items
• Suggest the best use of your time based on your goals and what's already on your calendar
• Support multiple calendars — just tell me which one to use

**Getting started**
Send me **/register** and I'll send you a link to connect your Google Calendar. Setup takes about 2 minutes.

**Your privacy**
• All personal data — your name, goals, calendar events, and conversation history — is encrypted in the database
• Your data is completely isolated from other users; no one else can see it
• Your Google Calendar access token is stored encrypted and is only used to make requests on your behalf
• You can delete your account and all associated data at any time by sending **/unregister**

**A note on how this works**
CaseBot is powered by Claude (Anthropic's AI). Your messages are sent to Anthropic's API to generate responses. See Anthropic's privacy policy at anthropic.com/privacy for how they handle data.

Send **/register** to get started.\
"""


@discord_service.client.event
async def on_member_join(member):
    if member.bot:
        return
    try:
        await member.send(WELCOME_MESSAGE.format(name=member.display_name))
        _log("👋", f"Sent welcome DM to {member} ({member.id})")
    except discord.errors.Forbidden:
        _log("⚠️ ", f"Could not DM {member} ({member.id}) — DMs may be disabled")
    except Exception:
        logger.exception(f"on_member_join failed for {member.id}")


@discord_service.client.event
async def on_message(message):
    if message.author.bot:
        return
    if not isinstance(message.channel, discord.DMChannel):
        return

    discord_id = str(message.author.id)
    text = (message.content or "").strip()

    if not text:
        if message.attachments:
            await discord_service.send_dm(
                "I can only read text right now — try typing your request.",
                discord_id, message.channel,
            )
        return

    # Commands handled before user lookup
    if text.lower() in ("/register", "register"):
        await _handle_register(message)
        return
    if text.lower() in ("/unregister", "unregister"):
        await _handle_unregister(message)
        return
    if text.lower() in ("/help", "help"):
        await _handle_help(message)
        return

    # Look up user
    conn = db.get_connection()
    try:
        user_ctx = db.get_user(conn, discord_id)
    finally:
        conn.close()

    if user_ctx is None:
        await discord_service.send_dm(
            "Hey! I don't have you registered yet. Send /register to get started.",
            discord_id, message.channel,
        )
        return

    # Onboarding: collect name and timezone if missing
    if user_ctx.name == "there":
        await _handle_onboarding(message, discord_id, user_ctx, text)
        return

    # Hard commands (require registered user)
    if text.lower() in ("/whitelist", "whitelist"):
        await _handle_whitelist_command(discord_id, message.channel)
        return
    if text.lower() in ("/backup", "backup"):
        await _handle_backup(message, discord_id, user_ctx)
        return
    if text.lower() in ("/registertest", "registertest") and _is_dev(discord_id):
        await _handle_registertest(discord_id, message.channel)
        return

    if _is_dev(discord_id):
        _log("💬", f'[{discord_id}] "{text[:80]}"')
    else:
        _log("💬", f"[{discord_id}] message received ({len(text)} chars)")

    async with message.channel.typing():
        conn = db.get_connection()
        extra_context = None
        stored_facts: list[str] = []

        try:
            # 1. Expand tag pool
            new_tags = await tag_manager.update_tag_pool(conn, discord_id, text)
            if new_tags:
                _log("🏷 ", f"New tags → {', '.join(new_tags)}")

            # 2. Classify + extract facts
            analysis = await analyst.analyze(text)
            classification = analysis.get("classification", "neither")
            _log("🔍", f"[{discord_id}] Classify → {classification}")

            if classification in ("factual", "both"):
                all_tags = db.get_all_tags(conn, discord_id)
                for fact_content in analysis.get("facts", []):
                    fact_id = db.insert_fact(conn, discord_id, fact_content, text)
                    assigned = await tagger.tag_fact(fact_content, all_tags)
                    if assigned:
                        tag_map = db.upsert_tags(conn, discord_id, assigned)
                        db.tag_fact(conn, fact_id, list(tag_map.values()))
                    tag_str = ", ".join(assigned) if assigned else "none"
                    if _is_dev(discord_id):
                        _log("📥", f"Fact #{fact_id}: \"{fact_content[:60]}\" [{tag_str}]")
                    else:
                        _log("📥", f"Fact #{fact_id} stored [{tag_str}]")
                    stored_facts.append(fact_content)

            # 3. Route
            recent = db.get_recent_conversation(conn, discord_id, 3)
            route = await listener.route(
                text,
                [{"role": t.role, "content": t.content} for t in recent],
            )
            needs_mem = route.get("needs_memory_query", False)
            needs_cal = route.get("needs_calendar", False)
            use_sonnet = route.get("use_sonnet", True)
            route_parts = [p for p, on in [("memory", needs_mem), ("calendar", needs_cal)] if on]
            tier = "sonnet" if use_sonnet else "haiku"
            _log("📡", f"Route → {' + '.join(route_parts) if route_parts else 'none'} [{tier}]")

            # 4. Memory query
            if needs_mem and route.get("query_tags"):
                qtags = route["query_tags"]
                _log("🔎", f"Querying memory: {', '.join(qtags)}")
                facts = db.get_facts_by_tags(conn, discord_id, qtags)
                if facts:
                    lines = [f"- {f['content']} [tags: {', '.join(f['tags'])}]" for f in facts]
                    extra_context = "Relevant facts from memory:\n" + "\n".join(lines)
                    _log("📎", f"{len(facts)} fact(s) injected into context")
                else:
                    _log("📎", "No matching facts found")

            # 5. Claude turn
            reply = await claude_service.run_claude_turn(conn, text, user_ctx, extra_context=extra_context, use_sonnet=use_sonnet)

            # Memory transparency footer
            if stored_facts:
                short = "; ".join(f[:80] for f in stored_facts[:3])
                if len(stored_facts) > 3:
                    short += f" (+{len(stored_facts) - 3} more)"
                reply = f"{reply}\n\n_Remembered: {short}_"

            _log("✉️ ", f"Reply ({len(reply)} chars)")
            option_menu = pop_pending_option_menu(discord_id)
            if option_menu:
                if reply:
                    await discord_service.send_dm(reply, discord_id, message.channel)
                lines = [option_menu["prompt"]]
                for i, opt in enumerate(option_menu["options"]):
                    lines.append(f"{NUMBER_EMOJIS[i]} {opt}")
                sent = await message.channel.send("\n".join(lines))
                for i in range(len(option_menu["options"])):
                    await sent.add_reaction(NUMBER_EMOJIS[i])
                _pending_option_menus[sent.id] = {
                    "discord_id": discord_id,
                    "options": option_menu["options"],
                    "channel_id": message.channel.id,
                }
            else:
                await discord_service.send_dm(reply, discord_id, message.channel)

        except anthropic.APIError:
            logger.exception("Anthropic API error")
            await discord_service.send_dm(
                "Claude is having trouble right now. Try again in a minute.",
                discord_id, message.channel,
            )
        except HttpError:
            logger.exception("Google API error")
            await discord_service.send_dm(
                "Calendar API hiccup — try again, or check Google's status if it persists.",
                discord_id, message.channel,
            )
        except Exception:
            logger.exception("Unhandled error in on_message")
            await discord_service.send_dm(
                "Something broke on my end. Logged it for review.",
                discord_id, message.channel,
            )
        finally:
            conn.close()


@discord_service.client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == discord_service.client.user.id:
        return

    message_id = payload.message_id

    # Whitelist switch menu — reaction add = enable
    if message_id in _pending_whitelist_toggle_menus:
        await _apply_whitelist_reaction(payload, enable=True)
        return

    # Multi-select calendar whitelist menu
    if message_id in _pending_multi_select_menus:
        menu = _pending_multi_select_menus[message_id]
        discord_id = str(payload.user_id)
        if discord_id != menu["discord_id"]:
            return
        emoji_str = str(payload.emoji)
        if emoji_str in NUMBER_EMOJIS:
            idx = NUMBER_EMOJIS.index(emoji_str)
            if idx < len(menu["calendars"]):
                menu["selected"].add(idx)
        elif emoji_str == CONFIRM_EMOJI:
            if not menu["selected"]:
                channel = discord_service.client.get_channel(payload.channel_id) or await discord_service.client.fetch_channel(payload.channel_id)
                await discord_service.send_dm("Select at least one calendar first, then react with ✅.", discord_id, channel)
                return
            del _pending_multi_select_menus[message_id]
            selected_gcal_ids = [menu["calendars"][i].gcal_id for i in sorted(menu["selected"])]
            conn = db.get_connection()
            try:
                db.set_calendar_whitelist(conn, discord_id, selected_gcal_ids)
            finally:
                conn.close()
            channel = discord_service.client.get_channel(payload.channel_id) or await discord_service.client.fetch_channel(payload.channel_id)
            _log("📋", f"[{discord_id}] Whitelisted {len(selected_gcal_ids)} calendar(s)")
            await _complete_onboarding(discord_id, channel)
        return

    if message_id not in _pending_option_menus:
        return

    menu = _pending_option_menus[message_id]
    discord_id = str(payload.user_id)
    if discord_id != menu["discord_id"]:
        return

    emoji_str = str(payload.emoji)
    if emoji_str not in NUMBER_EMOJIS:
        return

    idx = NUMBER_EMOJIS.index(emoji_str)
    if idx >= len(menu["options"]):
        return

    selected = menu["options"][idx]
    del _pending_option_menus[message_id]

    channel = discord_service.client.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await discord_service.client.fetch_channel(payload.channel_id)
        except Exception:
            return

    if _is_dev(discord_id):
        _log("🔢", f"[{discord_id}] Selected option {idx + 1}: {selected[:40]}")
    else:
        _log("🔢", f"[{discord_id}] Selected option {idx + 1}")

    async with channel.typing():
        conn = db.get_connection()
        try:
            user_ctx = db.get_user(conn, discord_id)
            if not user_ctx:
                return
            selection_text = f"[Selected: {selected}]"
            reply = await claude_service.run_claude_turn(conn, selection_text, user_ctx, use_sonnet=True)
            option_menu = pop_pending_option_menu(discord_id)
            if option_menu:
                if reply:
                    await discord_service.send_dm(reply, discord_id, channel)
                lines = [option_menu["prompt"]]
                for i, opt in enumerate(option_menu["options"]):
                    lines.append(f"{NUMBER_EMOJIS[i]} {opt}")
                sent = await channel.send("\n".join(lines))
                for i in range(len(option_menu["options"])):
                    await sent.add_reaction(NUMBER_EMOJIS[i])
                _pending_option_menus[sent.id] = {
                    "discord_id": discord_id,
                    "options": option_menu["options"],
                    "channel_id": channel.id,
                }
            else:
                await discord_service.send_dm(reply, discord_id, channel)
        except Exception:
            logger.exception("Error handling reaction selection")
            await discord_service.send_dm("Something went wrong. Please try again.", discord_id, channel)
        finally:
            conn.close()


@discord_service.client.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.user_id == discord_service.client.user.id:
        return
    if payload.message_id in _pending_whitelist_toggle_menus:
        await _apply_whitelist_reaction(payload, enable=False)


async def _apply_whitelist_reaction(payload: discord.RawReactionActionEvent, enable: bool):
    message_id = payload.message_id
    menu = _pending_whitelist_toggle_menus.get(message_id)
    if not menu:
        return

    discord_id = str(payload.user_id)
    if discord_id != menu["discord_id"]:
        return

    emoji_str = str(payload.emoji)
    if emoji_str not in NUMBER_EMOJIS:
        return

    idx = NUMBER_EMOJIS.index(emoji_str)
    if idx >= len(menu["calendars"]):
        return

    cal = menu["calendars"][idx]
    channel = (discord_service.client.get_channel(payload.channel_id)
               or await discord_service.client.fetch_channel(payload.channel_id))

    conn = db.get_connection()
    try:
        db.set_single_calendar_whitelist(conn, discord_id, cal.gcal_id, enable)
        user_ctx = db.get_user(conn, discord_id)
    finally:
        conn.close()

    if user_ctx:
        page_gcal_ids = [c.gcal_id for c in menu["calendars"]]
        order = {gcal_id: i for i, gcal_id in enumerate(page_gcal_ids)}
        fresh = [c for c in user_ctx.calendars if c.gcal_id in page_gcal_ids]
        fresh.sort(key=lambda c: order.get(c.gcal_id, 99))
        menu["calendars"] = fresh

    try:
        discord_msg = await channel.fetch_message(message_id)
        await discord_msg.edit(content=_whitelist_page_text(
            menu["calendars"], menu["page_num"], menu["total_pages"]
        ))
    except Exception:
        pass

    action = "enabled" if enable else "disabled"
    _log("📋", f"[{discord_id}] {cal.name} {action}")
    await discord_service.send_dm(f"{cal.name} {action}.", discord_id, channel)


HELP_TEXT = """\
CaseBot — commands and capabilities

Commands:
  /register    Connect your Google Calendar and create your account
  /unregister  Delete your account and all stored data (GDPR)
  /whitelist   Choose which calendars CaseBot can read and write
  /backup      Download your stored facts as a text file
  /help        Show this message

What I can do (just talk to me naturally):
  • Schedule, view, move, and cancel events on your Google Calendar
  • Check for conflicts before booking — warns on all-day/multi-day overlaps, blocks on hard conflicts
  • Remind you before events start and follow up afterward to mark them complete
  • Send a morning briefing every day at 8am with your schedule and any overdue items
  • Remember facts about you (goals, preferences, habits) and use them in responses
  • Suggest the best use of your time based on your goals and what's on your calendar
  • Update my own behavior — harshness level, reminder timing, follow-up style
  • Support multiple calendars — just say which one to use, or set a default via /whitelist

Tips:
  • When I show a numbered menu, react with the number emoji to make a selection
  • For /whitelist: react to enable a calendar, un-react to disable it
  • I use Claude (Anthropic) to understand your messages — see anthropic.com/privacy\
"""


async def _handle_help(message):
    await discord_service.send_dm(HELP_TEXT, str(message.author.id), message.channel)


async def _handle_register(message):
    discord_id = str(message.author.id)
    conn = db.get_connection()
    try:
        existing = db.get_user(conn, discord_id)
        if existing and existing.calendars:
            await discord_service.send_dm(
                "You're already registered! Use /unregister to remove your account.",
                discord_id, message.channel,
            )
            return
        # Create or reactivate user record (needed so OAuth callback can UPDATE the row)
        db.create_user(conn, discord_id, str(message.author))
    finally:
        conn.close()

    state = make_state_token(discord_id)
    auth_url = f"{settings.WEB_HOST}/auth/google?state={state}"
    await discord_service.send_dm(
        f"Let's get you set up! Click the link below to connect your Google Calendar:\n{auth_url}\n\n"
        "The link expires in 10 minutes.",
        discord_id, message.channel,
    )


async def _handle_unregister(message):
    discord_id = str(message.author.id)
    conn = db.get_connection()
    try:
        user_ctx = db.get_user(conn, discord_id)
        if not user_ctx:
            await discord_service.send_dm("You're not registered.", discord_id, message.channel)
            return
        db.delete_user(conn, discord_id)
    finally:
        conn.close()

    # Cancel all scheduled jobs for this user
    if scheduler_service.scheduler:
        for job in list(scheduler_service.scheduler.get_jobs()):
            if discord_id in job.id:
                scheduler_service.scheduler.remove_job(job.id)
    calendar_service.invalidate_service_cache(discord_id)
    await discord_service.send_dm(
        "Your account and all data have been deleted. Thanks for using CaseBot.",
        discord_id, message.channel,
    )


_ONBOARDING_STATE: dict[str, str] = {}  # discord_id → "name" | "timezone"

NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
CONFIRM_EMOJI = "✅"
_pending_option_menus: dict[int, dict] = {}  # message_id → {discord_id, options, channel_id}
_pending_multi_select_menus: dict[int, dict] = {}  # message_id → {discord_id, calendars, selected, channel_id}
_pending_whitelist_toggle_menus: dict[int, dict] = {}  # message_id → {discord_id, calendars, page_num, total_pages, channel_id}


async def _show_calendar_whitelist_menu(discord_id: str, channel):
    conn = db.get_connection()
    try:
        user_ctx = db.get_user(conn, discord_id)
    finally:
        conn.close()

    all_calendars = user_ctx.calendars if user_ctx else []
    if not all_calendars:
        await _complete_onboarding(discord_id, channel)
        return

    shown = all_calendars[:9]
    overflow = len(all_calendars) - len(shown)

    lines = [
        "Which calendars should I have access to? You must enable at least one.",
        "React with the number(s) to select them, then ✅ to confirm.\n",
    ]
    for i, cal in enumerate(shown):
        marker = " (primary)" if cal.is_default else ""
        lines.append(f"{NUMBER_EMOJIS[i]} {cal.name}{marker}")
    if overflow:
        lines.append(f"\n({overflow} more calendar(s) — use /whitelist after setup to manage all of them)")

    msg = await channel.send("\n".join(lines))
    for i in range(len(shown)):
        await msg.add_reaction(NUMBER_EMOJIS[i])
    await msg.add_reaction(CONFIRM_EMOJI)

    _pending_multi_select_menus[msg.id] = {
        "discord_id": discord_id,
        "calendars": shown,
        "selected": set(),
        "channel_id": channel.id,
    }


async def _handle_backup(message, discord_id: str, user_ctx):
    import io
    from datetime import date as _date
    await discord_service.send_dm("Preparing your backup...", discord_id, message.channel)
    conn = db.get_connection()
    try:
        facts = db.get_all_facts(conn, discord_id)
    finally:
        conn.close()
    if not facts:
        await discord_service.send_dm("No facts stored yet — nothing to back up.", discord_id, message.channel)
        return
    async with message.channel.typing():
        text = await claude_service.generate_facts_backup(user_ctx, facts)
    filename = f"casebot_backup_{_date.today().isoformat()}.txt"
    await message.channel.send(
        f"Backup ready — {len(facts)} fact(s) on record.",
        file=discord.File(io.BytesIO(text.encode("utf-8")), filename=filename),
    )
    _log("💾", f"[{discord_id}] /backup sent ({len(facts)} facts)")


async def _handle_registertest(discord_id: str, channel):
    conn = db.get_connection()
    try:
        user_ctx = db.get_user(conn, discord_id)
        if user_ctx:
            try:
                calendar_service.sync_user_calendars(conn, user_ctx)
            except Exception:
                logger.exception("Calendar sync failed during registertest")
        db.set_calendar_whitelist(conn, discord_id, [])
    finally:
        conn.close()
    _log("🧪", f"[{discord_id}] /registertest — whitelist reset, showing onboarding menu")
    await discord_service.send_dm(
        "[DEV] Simulating registration — all calendars reset. Showing whitelist setup.",
        discord_id, channel,
    )
    await _show_calendar_whitelist_menu(discord_id, channel)


def _whitelist_page_text(calendars, page_num: int, total_pages: int) -> str:
    header = "Calendars — react to toggle whitelist access:"
    if total_pages > 1:
        header += f" (page {page_num}/{total_pages})"
    lines = [header, ""]
    for i, cal in enumerate(calendars):
        status = "✅" if cal.whitelisted else "❌"
        marker = " (primary)" if cal.is_default else ""
        lines.append(f"{NUMBER_EMOJIS[i]} {status} {cal.name}{marker}")
    return "\n".join(lines)


async def _handle_whitelist_command(discord_id: str, channel):
    conn = db.get_connection()
    try:
        user_ctx = db.get_user(conn, discord_id)
    finally:
        conn.close()

    if not user_ctx or not user_ctx.calendars:
        await discord_service.send_dm("No calendars found. Try /register first.", discord_id, channel)
        return

    calendars = user_ctx.calendars
    chunks = [calendars[i:i + 9] for i in range(0, len(calendars), 9)]
    total_pages = len(chunks)

    for page_num, chunk in enumerate(chunks, start=1):
        text = _whitelist_page_text(chunk, page_num, total_pages)
        msg = await channel.send(text)
        for i in range(len(chunk)):
            await msg.add_reaction(NUMBER_EMOJIS[i])
        _pending_whitelist_toggle_menus[msg.id] = {
            "discord_id": discord_id,
            "calendars": chunk,
            "page_num": page_num,
            "total_pages": total_pages,
            "channel_id": channel.id,
        }
    _log("📋", f"[{discord_id}] /whitelist menu sent ({len(calendars)} calendars, {total_pages} page(s))")


async def _complete_onboarding(discord_id: str, channel):
    conn = db.get_connection()
    try:
        user_ctx = db.get_user(conn, discord_id)
    finally:
        conn.close()
    tz = user_ctx.timezone if user_ctx else "your timezone"
    await discord_service.send_dm(
        f"All set! You'll get a morning briefing at 8am {tz} every day.\n"
        "You can now start chatting with me. Try: \"What's on my calendar this week?\"",
        discord_id, channel,
    )


async def _handle_onboarding(message, discord_id: str, user_ctx, text: str):
    state = _ONBOARDING_STATE.get(discord_id, "name")

    if state == "name":
        conn = db.get_connection()
        try:
            db.update_user_profile(conn, discord_id, name=text.strip())
        finally:
            conn.close()
        _ONBOARDING_STATE[discord_id] = "timezone"
        await discord_service.send_dm(
            f"Got it, {text.strip()}! Now, what's your timezone?\n"
            "Examples: America/New_York, America/Chicago, America/Los_Angeles, Europe/London\n"
            "Full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones",
            discord_id, message.channel,
        )
    elif state == "timezone":
        # Basic validation
        try:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            ZoneInfo(text.strip())
        except Exception:
            await discord_service.send_dm(
                f"I didn't recognize \"{text.strip()}\" as a timezone. "
                "Try something like America/New_York or Europe/London.",
                discord_id, message.channel,
            )
            return
        conn = db.get_connection()
        try:
            db.update_user_profile(conn, discord_id, timezone=text.strip())
            user_ctx = db.get_user(conn, discord_id)
        finally:
            conn.close()
        _ONBOARDING_STATE.pop(discord_id, None)
        scheduler_service.schedule_morning_briefing(discord_id, text.strip())
        await _show_calendar_whitelist_menu(discord_id, message.channel)


def _run_web():
    config = uvicorn.Config(web_app, host="0.0.0.0", port=settings.WEB_PORT, log_level="warning")
    server = uvicorn.Server(config)
    asyncio.run(server.serve())


async def main():
    db.init_db()
    _log("🗄 ", "Database ready")

    # Start the OAuth web server in a background thread
    web_thread = threading.Thread(target=_run_web, daemon=True)
    web_thread.start()
    _log("🌐", f"Web server started on port {settings.WEB_PORT}")

    await discord_service.client.start(settings.DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
