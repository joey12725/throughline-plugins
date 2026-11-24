-- Postgres schema read by the beeper plugin's `archive` source
-- (BEEPER_SOURCE=archive). Use it to import chat history you already hold in
-- another system: load your data into these tables (a view with the same
-- columns works too) and point BEEPER_ARCHIVE_DSN at a read-only role with
-- USAGE on schema archive and SELECT on these tables.
--
-- Everything is text so an exporter can write values as it found them; empty
-- strings stand for "unknown". Timestamps are ISO 8601 (with offset where
-- known). JSON columns hold JSON text.

CREATE SCHEMA IF NOT EXISTS archive;

-- One row per chat.
CREATE TABLE archive.conversations (
    id text PRIMARY KEY,           -- stable conversation id, referenced by messages
    platform text NOT NULL,        -- network: discord, instagram, sms, telegram, ...
    native_id text,                -- the network's own chat id
    title text,                    -- chat title
    kind text,                     -- dm, group, channel, ...
    guild_id text,                 -- server/guild id where the network has one
    metadata_json text,            -- free JSON object, kept in thread metadata
    history_status text,           -- optional: complete/partial/... reported as coverage
    history_detail text            -- optional: JSON object with coverage detail
);

-- One row per message.
CREATE TABLE archive.messages (
    id text PRIMARY KEY,           -- stable message id, becomes the external id
    platform text NOT NULL,
    conversation_id text,          -- archive.conversations.id
    native_id text,                -- the network's own message id
    sender_id text,                -- the network's sender id
    sender_name text,
    direction text,                -- incoming / outgoing
    sent_at text,                  -- ISO time; '' when unknown
    time_raw text,                 -- the original time text, used when sent_at is empty
    time_precision text,           -- exact, minute, day, ...
    body text,
    edited_at text,
    reply_to text,                 -- archive.messages.id of the replied-to message
    attachments_json text,         -- JSON list of {fileName, type, url, proxy_url, ...}
    reactions_json text,           -- JSON list of {emoji, sender_id, sender_name, ...}
    metadata_json text,            -- free JSON object
    identity_quality text,         -- how sender_id was established (native, ...)
    first_imported_at text,
    last_seen_at text
);

-- Optional: what kind of identifier a sender id is, per network.
CREATE TABLE archive.identities (
    identity_id text PRIMARY KEY,
    platform text NOT NULL,
    namespace text NOT NULL,       -- e.g. user_id, phone, username
    native_id text NOT NULL        -- matches archive.messages.sender_id
);

-- Optional: other ids the same message is known by (for example the id a
-- live Beeper run would give it), so live and archive runs meet.
CREATE TABLE archive.message_aliases (
    message_id text NOT NULL,      -- archive.messages.id
    platform text NOT NULL,
    native_id text NOT NULL
);

-- Optional, speeds up the streaming read for large archives.
CREATE INDEX ON archive.messages
    ((COALESCE(conversation_id, '') COLLATE "C"), (COALESCE(sent_at, '') COLLATE "C"), (id COLLATE "C"));
