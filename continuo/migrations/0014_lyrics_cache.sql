-- Lyrics from LRCLIB for songs the library has none for (and Play now songs): a song's words
-- don't change, so a found answer is kept; "none" is kept a week, then asked again.
CREATE TABLE lyrics_cache (
    key         TEXT PRIMARY KEY,           -- normalised artist|title|duration in seconds
    body        TEXT,                       -- the JSON answer; NULL: LRCLIB had none
    fetched_at  TEXT NOT NULL
);
