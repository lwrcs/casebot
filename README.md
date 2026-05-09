# CaseBot

A personal planning assistant that lives in your Discord DMs. Backed by Google Calendar, a local SQLite database, and Claude (Anthropic API). Sends proactive reminders, tracks completion, remembers facts about you, and can push back when you're slacking.

---

## What it does

**Schedule management**
- Create, view, and update Google Calendar events via natural language
- Checks for conflicts before creating events and asks for confirmation before overriding them
- Marks events as completed, incomplete, rescheduled, or cancelled

**Proactive reminders**
- Pre-event reminder (default: 30 minutes before)
- Follow-up after events end asking if you completed them — tone adjusts to harshness setting
- Morning briefing at 8am with today's schedule and any overdue items
- All reminders survive bot restarts and are recovered on startup

**Persistent memory**
- Extracts factual statements from every message (goals, preferences, habits, project details)
- Tags each fact and stores it in a local database
- Recalls relevant facts automatically when answering questions
- Shows you a footer confirming what was remembered after each message where facts were stored

**Goal awareness**
- Reads `goals.md` at startup; Claude references it when suggesting how to spend time
- `suggest_best_use_of_time` tool analyzes your calendar, recent completion rate, and goals to recommend priorities

**Adjustable personality**
- Change persistence, harshness, motivational style, and reminder timing via natural language ("be more harsh", "remind me 15 minutes early")
- Settings persist in `behavior.json`

**Calendar sync**
- Polls Google Calendar every 5 minutes for external changes
- Detects manual deletions, additions, and edits
- Uses Google's `syncToken` for efficient incremental sync; handles token expiration with a full resync

---

## Architecture

```
main.py
├── on_message pipeline:
│   ├── agents/tag_manager.py    — expand tag pool with new semantic tags
│   ├── agents/analyst.py        — classify message + extract facts
│   ├── agents/tagger.py         — assign tags to extracted facts
│   ├── agents/listener.py       — decide if memory query / calendar needed
│   └── services/claude_service.py — main Claude turn + tool use loop
│
├── services/scheduler_service.py  — APScheduler jobs (reminders, briefing, sync)
├── services/calendar_service.py   — Google Calendar API wrapper
├── services/discord_service.py    — Discord client + send helper
├── db/database.py                 — SQLite CRUD
├── tools/definitions.py           — Claude tool schemas + handlers
├── agents/_base.py                — shared agent infrastructure
├── config.py                      — settings loaded from .env
└── utils.py                       — timezone helpers
```

### Message pipeline (per message)

1. **Tag pool update** — `tag_manager` reviews the message and proposes new tags to add to the pool, deduplicating semantically (won't add "musical" if "music" exists)
2. **Classify + extract** — `analyst` classifies the message as `scheduling` / `factual` / `both` / `neither` and extracts discrete facts
3. **Store facts** — facts are inserted into the DB and tagged via `tagger`
4. **Route** — `listener` decides whether the main Claude turn needs a memory lookup, calendar context, or neither
5. **Memory query** — if memory is needed, relevant facts are fetched by tag and injected into the system prompt
6. **Claude turn** — `claude_service.run_claude_turn` runs Claude with tools in a loop (up to 5 iterations) until `end_turn`
7. **Reply** — response sent to Discord; if facts were stored, a `_Remembered: ..._` footer is appended

### Tool use loop

Claude has 6 tools. The loop calls the API, handles `tool_use` stop reasons by dispatching to handlers, appends results, and repeats until `end_turn` or 5 iterations.

| Tool | Purpose |
|------|---------|
| `get_calendar_events` | List events in a time range |
| `create_calendar_event` | Create event (conflict check, DB insert, schedule reminders) |
| `update_event_status` | Mark complete / incomplete / rescheduled / cancelled |
| `check_availability` | Freebusy query for a time window |
| `update_behavior` | Patch behavior.json settings |
| `suggest_best_use_of_time` | Return goals + completion rate + busy slots for prioritization |

---

## Setup

### Prerequisites

- Python 3.10+
- A Discord bot token with DM permissions and `message_content` intent enabled
- Google Cloud project with Calendar API enabled and OAuth 2.0 Desktop credentials downloaded as `credentials.json`
- Anthropic API key

### Install

```bash
pip install -r requirements.txt
```

### Configure

Create a `.env` file in the project root:

```env
ANTHROPIC_API_KEY=sk-ant-...
DISCORD_BOT_TOKEN=...
DISCORD_USER_ID=274049204471595009
GOOGLE_CREDENTIALS_FILE=./credentials.json
GOOGLE_TOKEN_FILE=./token.json
GOOGLE_CALENDAR_ID=primary
DATABASE_PATH=./casebot.db
BEHAVIOR_FILE=./behavior.json
GOALS_FILE=./goals.md
CONVERSATION_HISTORY_TURNS=20
USER_TIMEZONE=America/New_York
```

Only `ANTHROPIC_API_KEY`, `DISCORD_BOT_TOKEN`, and `DISCORD_USER_ID` are required. The rest have defaults.

### Google OAuth

First run will open a browser for OAuth authorization. After approval, `token.json` is written and reused automatically. If your app is in "testing" mode in Google Cloud Console, add your Google account as a test user under OAuth consent screen → Audience → Test users.

### Goals file

Create `goals.md` with your goals and active projects. Claude reads this at startup and references it for planning suggestions. Update it anytime; changes take effect on the next restart (or call `reload_goals_doc()` in the service).

### First run

```bash
python main.py
```

The bot will authenticate with Google Calendar, initialize the SQLite database, and start listening for DMs. Send any message to the bot's Discord account to start.

---

## Configuration

`behavior.json` is created automatically on first run with defaults. You can change any value by telling the bot in plain language.

| Field | Default | Description |
|-------|---------|-------------|
| `persistence_level` | 5 | 1–10 how hard to push on incomplete tasks |
| `harshness_level` | 5 | 1–10 tone of follow-ups and reminders |
| `motivational_style` | `"balanced"` | `"gentle"` / `"balanced"` / `"harsh"` |
| `reminder_advance_minutes` | 30 | How early to send pre-event reminders |
| `follow_up_incomplete` | true | Whether to send post-event follow-ups |
| `follow_up_delay_hours` | 2 | Hours after event end before follow-up fires |

---

## Database schema

SQLite at `casebot.db` (WAL mode, foreign keys on).

| Table | Purpose |
|-------|---------|
| `events` | Calendar events with local status tracking |
| `event_history` | Audit log of every status change |
| `conversation_history` | Last N turns fed to Claude as context |
| `scheduled_reminders` | Pending reminder jobs with sent tracking |
| `facts` | Extracted factual statements about the user |
| `tags` | Tag vocabulary (auto-managed) |
| `fact_tags` | Many-to-many: fact ↔ tag |
| `calendar_sync` | Stores Google Calendar `syncToken` |

---

## Reliability notes

- **Scheduler jobs** are wrapped in `try/except`; a failed reminder logs the error and doesn't crash the scheduler
- **Reminders** are only marked sent after successful Discord delivery
- **Calendar event creation** is atomic: if the DB insert fails after Google Calendar creation, the GCal event is deleted to prevent drift
- **Config files** (`behavior.json`, `goals.md`) have fallbacks — missing or corrupt files produce warnings and use defaults rather than crashes
- **API retries** — Anthropic 529 overload errors trigger async retries (5s, 15s, 30s) without blocking the Discord event loop; agent pipeline uses shorter delays (3s, 10s)
- **Restart recovery** — `recover_unsent_reminders` runs on startup and re-registers any future reminders that were scheduled but not yet sent

---

## File reference

```
main.py                      — entry point, Discord event handlers
config.py                    — settings dataclass, loaded from .env
utils.py                     — timezone helpers (now_local, to_local, local_offset_str)
goals.md                     — your goals / projects (edit freely)
behavior.json                — bot personality settings (auto-created)
requirements.txt             — Python dependencies

agents/
  _base.py                   — shared Haiku call + JSON extraction + retry
  analyst.py                 — message classifier + fact extractor
  tag_manager.py             — tag pool manager (semantic dedup)
  tagger.py                  — assigns tags to extracted facts
  listener.py                — routes message (memory? calendar?)

db/
  database.py                — all SQLite operations
  models.py                  — dataclasses: Event, ConversationTurn, ScheduledReminder

services/
  calendar_service.py        — Google Calendar API: list, create, availability, sync
  claude_service.py          — Claude turn loop, system prompt builder, conversation history
  discord_service.py         — Discord client, send_dm helper
  scheduler_service.py       — APScheduler: reminders, briefing, calendar sync

tools/
  definitions.py             — Claude tool schemas + handler functions
```
