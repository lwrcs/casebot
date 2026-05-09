-- Encrypt sensitive fields that were previously plaintext.
-- name, goals, behavior in users; notes and goal_connection in events.

ALTER TABLE users
    ALTER COLUMN name TYPE BYTEA USING name::BYTEA,
    ALTER COLUMN goals TYPE BYTEA USING goals::BYTEA,
    ALTER COLUMN behavior DROP DEFAULT,
    ALTER COLUMN behavior TYPE BYTEA USING behavior::TEXT::BYTEA;

ALTER TABLE events
    ALTER COLUMN notes TYPE BYTEA USING notes::BYTEA,
    ALTER COLUMN goal_connection TYPE BYTEA USING goal_connection::BYTEA;
