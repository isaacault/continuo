-- Racing slow downloads (#31): a second source started for a request whose download is slow.
-- The first one Lidarr imports wins; the others are cancelled.
CREATE TABLE races (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id     INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    source         TEXT NOT NULL,                 -- soulseek
    username       TEXT,
    remote_folder  TEXT,
    local_folder   TEXT,                          -- slskd saves under its downloads/<this>
    files          TEXT NOT NULL DEFAULT '[]',    -- [{filename, size}]
    state          TEXT NOT NULL,                 -- downloading | importing | won | lost | failed
    detail         TEXT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT
);
CREATE INDEX races_request ON races (request_id);
