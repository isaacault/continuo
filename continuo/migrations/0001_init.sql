-- Schema v1: lidseeker's users/requests (extended) + devices, request_events, library_albums,
-- notifications, settings. Timestamps are ISO-8601 UTC strings.

CREATE TABLE users (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    username           TEXT NOT NULL UNIQUE,
    password_hash      TEXT NOT NULL,
    role               TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    navidrome_username TEXT,                 -- NULL = same as username
    token_version      INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL
);

-- One row per signed-in app install. Revoking a device kills its API refresh token AND its
-- façade secret, i.e. both requests and playback.
CREATE TABLE devices (
    id                 TEXT PRIMARY KEY,     -- dev_<random>
    user_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name               TEXT NOT NULL,
    platform           TEXT,
    refresh_hash       TEXT,                 -- sha256 of the current refresh token
    prev_refresh_hash  TEXT,                 -- the one it replaced: presenting it again = theft
    refresh_expires_at TEXT,
    facade_secret      TEXT NOT NULL,        -- plaintext: Subsonic token auth needs it (md5(secret+salt))
    push_kind          TEXT,
    push_endpoint      TEXT,
    created_at         TEXT NOT NULL,
    last_seen_at       TEXT,
    revoked_at         TEXT
);
CREATE INDEX idx_devices_user ON devices(user_id);

-- Statuses: pending, searching, downloading, importing, scanning, available, error,
-- needs_attention. There is deliberately no terminal "failed": a request is never given up on.
CREATE TABLE requests (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                 TEXT NOT NULL DEFAULT 'album',
    mb_release_group_id  TEXT NOT NULL,
    title                TEXT,
    artist               TEXT,
    year                 INTEGER,
    artwork_url          TEXT,
    note                 TEXT,
    lidarr_artist_id     INTEGER,
    lidarr_album_id      INTEGER,
    lidarr_release_id    TEXT,               -- the monitored release's MBID (foreignReleaseId)
    navidrome_album_id   TEXT,
    status               TEXT NOT NULL DEFAULT 'pending',
    still_looking        INTEGER NOT NULL DEFAULT 0,   -- display flag only, never terminal
    search_cycles        INTEGER NOT NULL DEFAULT 0,   -- fruitless cycles since the last progress
    last_cycle_at        TEXT,
    last_search_at       TEXT,                         -- last AlbumSearch we (or torrent-cleanup) fired
    last_source          TEXT,                         -- torrent | soulseek
    import_paths         TEXT,                         -- JSON list from the Lidarr webhook
    imported_at          TEXT,
    scan_started_at      TEXT,
    attention            TEXT,                         -- JSON {reason, detail, candidates}
    error                TEXT,
    user_id              INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    available_at         TEXT,
    notified_at          TEXT,
    UNIQUE (kind, mb_release_group_id)
);
CREATE INDEX idx_requests_status ON requests(status);
CREATE INDEX idx_requests_user ON requests(user_id);
CREATE INDEX idx_requests_lidarr_album ON requests(lidarr_album_id);

-- Append-only audit trail per request: transitions, webhook hits, searches, matching decisions.
CREATE TABLE request_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    at         TEXT NOT NULL,
    type       TEXT NOT NULL,
    detail     TEXT                          -- JSON
);
CREATE INDEX idx_request_events_request ON request_events(request_id, id);

-- Navidrome album index for matching. Disposable: rebuilt from Navidrome.
CREATE TABLE library_albums (
    navidrome_id         TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    artist               TEXT,
    year                 INTEGER,
    song_count           INTEGER,
    mbz_album_id         TEXT,               -- release MBID
    mbz_release_group_id TEXT,
    folder               TEXT,               -- album directory relative to the music root
    norm_key             TEXT,               -- normalise(artist)|normalise(name)
    source_updated_at    TEXT,               -- Navidrome's updatedAt, to refresh incrementally
    updated_at           TEXT NOT NULL
);
CREATE INDEX idx_library_rg ON library_albums(mbz_release_group_id);
CREATE INDEX idx_library_release ON library_albums(mbz_album_id);
CREATE INDEX idx_library_norm ON library_albums(norm_key);
CREATE INDEX idx_library_folder ON library_albums(folder);

CREATE TABLE notifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type          TEXT NOT NULL,
    title         TEXT NOT NULL,
    body          TEXT,
    action        TEXT,                      -- JSON
    request_id    INTEGER REFERENCES requests(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL,
    read_at       TEXT,
    delivered_via TEXT                       -- comma list: sse, homeassistant
);
CREATE INDEX idx_notifications_user ON notifications(user_id, id);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT                               -- JSON
);
