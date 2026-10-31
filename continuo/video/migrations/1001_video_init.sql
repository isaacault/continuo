-- The video part (docs/design/video.md): its own tables, created only when CONTINUO_MODULES lists
-- "video". Versions 1000+ so they never collide with core's and music's.

-- Jellyfin's library, as continuo matches it against TMDB: movies and series, by provider IDs.
CREATE TABLE video_library (
    jellyfin_id     TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,              -- movie | series
    title           TEXT NOT NULL,
    year            INTEGER,
    tmdb_id         INTEGER,
    tvdb_id         INTEGER,
    imdb_id         TEXT,
    updated_at      TEXT NOT NULL
);
CREATE INDEX video_library_tmdb ON video_library (kind, tmdb_id);

-- Film and TV requests: the same states as music's (requested, searching, downloading,
-- importing, available, needs_attention, failed), owned by the video part.
CREATE TABLE video_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,              -- movie | series
    tmdb_id         INTEGER NOT NULL,
    title           TEXT,
    year            INTEGER,
    poster_path     TEXT,
    seasons         TEXT,                       -- JSON list of season numbers (series), null = all
    status          TEXT NOT NULL DEFAULT 'requested',
    radarr_movie_id INTEGER,
    sonarr_series_id INTEGER,
    jellyfin_id     TEXT,
    attention       TEXT,
    error           TEXT,
    user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    available_at    TEXT,
    notified_at     TEXT
);
CREATE INDEX video_requests_status ON video_requests (status);

-- Each continuo account's Jellyfin user (created or linked on first sign-in).
CREATE TABLE video_accounts (
    user_id          INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    jellyfin_user_id TEXT NOT NULL,
    jellyfin_username TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

-- TMDB answers on disk (titles change rarely; the rate limit is generous but not free).
CREATE TABLE video_tmdb_cache (
    key        TEXT PRIMARY KEY,
    body       TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
