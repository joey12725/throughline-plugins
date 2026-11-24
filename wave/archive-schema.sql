-- Postgres schema read by the wave plugin's `archive` source (WAVE_SOURCE=archive).
-- Use it to import transcripts you already hold elsewhere: load them into
-- these tables and point WAVE_ARCHIVE_DSN at a read-only role with USAGE on
-- schema transcripts and SELECT on recording, turn and speaker. Only rows with
-- source = 'wave' are read. The anchor table is optional.

CREATE SCHEMA transcripts;
CREATE TABLE transcripts.recording (
 id text PRIMARY KEY, source text NOT NULL, source_id text NOT NULL, title text,
 started_at timestamptz, started_at_from text NOT NULL, uploaded_at timestamptz,
 duration_s numeric, device text, source_platform text, place text,
 lat double precision, lon double precision, ignore_reason text, flagged_by text,
 flagged_at timestamptz, metadata jsonb NOT NULL,
 imported_at timestamptz NOT NULL, updated_at timestamptz NOT NULL
);
CREATE TABLE transcripts.turn (
 recording_id text NOT NULL, turn_no integer NOT NULL, speaker text,
 start_s numeric, end_s numeric, text text NOT NULL, said_at timestamptz,
 said_at_manual timestamptz, place text, lat double precision, lon double precision,
 ignore_reason text, flagged_by text, flagged_at timestamptz, metadata jsonb NOT NULL,
 tsv tsvector, PRIMARY KEY(recording_id, turn_no)
);
CREATE TABLE transcripts.speaker (
 recording_id text NOT NULL, label text NOT NULL, original_label text,
 system text, kind text, value text, provenance text,
 PRIMARY KEY(recording_id, label)
);
CREATE TABLE transcripts.anchor (
 recording_id text, offset_s numeric, real_at timestamptz, note text, set_by text
);
