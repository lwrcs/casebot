-- CaseBot — initial PostgreSQL schema
-- Run once against a fresh database.

CREATE TABLE IF NOT EXISTS users (
    discord_id          TEXT PRIMARY KEY,
    discord_username    TEXT,
    name                TEXT,
    timezone            TEXT NOT NULL DEFAULT 'America/New_York',
    goals               TEXT,
    behavior            JSONB NOT NULL DEFAULT '{}',
    google_token_enc    BYTEA,
    google_calendar_id  TEXT NOT NULL DEFAULT 'primary',
    registered_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    active              BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS events (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
    gcal_event_id   TEXT,
    title           BYTEA NOT NULL,
    description     BYTEA,
    start_time      TEXT NOT NULL,
    end_time        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'scheduled',
    goal_connection TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, gcal_event_id)
);

CREATE TABLE IF NOT EXISTS event_history (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
    event_id    BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    action      TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    actor       TEXT NOT NULL DEFAULT 'system',
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS conversation_history (
    id        BIGSERIAL PRIMARY KEY,
    user_id   TEXT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
    role      TEXT NOT NULL,
    content   BYTEA NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS scheduled_reminders (
    id            BIGSERIAL PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
    event_id      BIGINT REFERENCES events(id) ON DELETE CASCADE,
    reminder_type TEXT NOT NULL,
    scheduled_for TIMESTAMPTZ NOT NULL,
    sent          BOOLEAN NOT NULL DEFAULT false,
    sent_at       TIMESTAMPTZ,
    job_id        TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id         BIGSERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
    content    BYTEA NOT NULL,
    source     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tags (
    id      BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
    name    TEXT NOT NULL,
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS fact_tags (
    fact_id BIGINT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    tag_id  BIGINT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (fact_id, tag_id)
);

CREATE TABLE IF NOT EXISTS calendar_sync (
    user_id TEXT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_events_user_start    ON events(user_id, start_time);
CREATE INDEX IF NOT EXISTS idx_events_user_status   ON events(user_id, status);
CREATE INDEX IF NOT EXISTS idx_reminders_user_due   ON scheduled_reminders(user_id, scheduled_for, sent);
CREATE INDEX IF NOT EXISTS idx_conversation_user_ts ON conversation_history(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_facts_user           ON facts(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_fact_tags_tag        ON fact_tags(tag_id);
