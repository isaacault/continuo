-- MusicBrainz answers kept on disk: tracklists, recordings, albums and artist names hardly
-- change, and MusicBrainz sheds load with 503s at busy times. Fresh entries are used instead of
-- asking; stale ones are refreshed, and used as they are when MusicBrainz can't be asked.
CREATE TABLE musicbrainz_cache (
    key         TEXT PRIMARY KEY,           -- path?sorted params
    body        TEXT NOT NULL,              -- the JSON answer
    bytes       INTEGER NOT NULL,
    fetched_at  TEXT NOT NULL,
    used_at     TEXT NOT NULL
);
CREATE INDEX musicbrainz_cache_used ON musicbrainz_cache (used_at);
