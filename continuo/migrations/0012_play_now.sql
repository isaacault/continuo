-- Play now (#12): a song you don't have, fetched from Soulseek (fastest copy) and played while it
-- downloads. One row per stream; the finished file goes to the media cache (origin 'stream').
CREATE TABLE streams (
    id                 TEXT PRIMARY KEY,           -- uuid, in the app's track id
    user_id            INTEGER REFERENCES users(id) ON DELETE CASCADE,
    recording_id       TEXT NOT NULL,              -- MusicBrainz recording
    album_rg           TEXT,                       -- its album's release group (for Keep)
    title              TEXT,
    artist             TEXT,
    duration_ms        INTEGER,
    status             TEXT NOT NULL,              -- searching | downloading | streaming | ready | failed
    username           TEXT,                       -- the Soulseek copy being fetched
    filename           TEXT,
    size               INTEGER,
    extension          TEXT,
    tried              TEXT,                       -- JSON [[user, file], ...] and refused users
    cache_item_id      INTEGER,
    error              TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX streams_recording ON streams (recording_id, status);
