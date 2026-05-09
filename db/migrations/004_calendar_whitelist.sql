ALTER TABLE user_calendars ADD COLUMN IF NOT EXISTS whitelisted BOOLEAN NOT NULL DEFAULT false;

-- Existing calendars are considered approved to preserve current behavior
UPDATE user_calendars SET whitelisted = true;
