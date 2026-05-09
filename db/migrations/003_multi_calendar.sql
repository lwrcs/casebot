CREATE TABLE IF NOT EXISTS user_calendars (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
    gcal_id     TEXT NOT NULL,
    name        BYTEA NOT NULL,
    color       TEXT,
    is_default  BOOLEAN NOT NULL DEFAULT false,
    UNIQUE (user_id, gcal_id)
);

CREATE INDEX IF NOT EXISTS idx_user_calendars_user ON user_calendars(user_id);
