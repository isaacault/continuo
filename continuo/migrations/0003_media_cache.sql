-- The media cache (issue #23): albums removed from the library now; streamed and prefetched
-- music later. Files live under <MUSIC_DIR>/.continuo-cache/<id>/; evicted least recently used.
CREATE TABLE media_cache (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                  TEXT NOT NULL CHECK (kind IN ('album', 'track')),
    origin                TEXT NOT NULL CHECK (origin IN ('removed', 'stream', 'prefetch')),
    title                 TEXT,
    artist                TEXT,
    mb_release_group_id   TEXT,
    mb_release_id         TEXT,
    mb_recording_id       TEXT,
    navidrome_album_id    TEXT,               -- the library album it was (removed)
    library_path          TEXT,               -- where it came from, relative to MUSIC_DIR
    cache_path            TEXT NOT NULL,      -- relative to the cache dir
    lidarr_album_id       INTEGER,
    lidarr_was_monitored  INTEGER NOT NULL DEFAULT 0,
    bytes                 INTEGER NOT NULL DEFAULT 0,
    removed_by            TEXT,
    created_at            TEXT NOT NULL,
    last_used_at          TEXT NOT NULL
);
CREATE INDEX media_cache_lru ON media_cache (last_used_at);
