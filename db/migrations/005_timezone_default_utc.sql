-- Change timezone default to UTC so new rows are distinguishable from
-- an explicitly-set timezone (auto-detected from Google Calendar).
ALTER TABLE users ALTER COLUMN timezone SET DEFAULT 'UTC';
