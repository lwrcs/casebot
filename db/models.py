from dataclasses import dataclass, field


@dataclass
class CalendarInfo:
    gcal_id: str
    name: str
    color: str | None = None
    is_default: bool = False
    whitelisted: bool = False


@dataclass
class UserContext:
    discord_id: str
    name: str
    timezone: str
    behavior: dict
    goals: str
    calendar_id: str  # default calendar gcal_id
    calendars: list[CalendarInfo] = field(default_factory=list)


@dataclass
class Event:
    user_id: str
    title: str
    start_time: str
    end_time: str
    id: int | None = None
    gcal_event_id: str | None = None
    description: str | None = None
    status: str = "scheduled"
    goal_connection: str | None = None
    notes: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class ConversationTurn:
    user_id: str
    role: str
    content: str
    id: int | None = None
    timestamp: str | None = None


@dataclass
class ScheduledReminder:
    user_id: str
    reminder_type: str
    scheduled_for: str
    id: int | None = None
    event_id: int | None = None
    sent: bool = False
    sent_at: str | None = None
    job_id: str | None = None


@dataclass
class Fact:
    user_id: str
    content: str
    id: int | None = None
    source: str | None = None
    created_at: str | None = None
    tags: list[str] = field(default_factory=list)
