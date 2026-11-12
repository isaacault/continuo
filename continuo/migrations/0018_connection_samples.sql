-- Each phone's connection while it streamed (polyphony#185 phase 2, #123): one row per sample of
-- about 30 s, as Polyphony's ConnectionSample sends it (POST /v1/devices/{id}/connection). Kept a
-- few weeks (continuo.db.connection_samples.KEEP_DAYS), for GET /v1/admin/connection and, later,
-- per-device suggestions for the stall thresholds (polyphony#14). Times are the phone's epoch ms.
CREATE TABLE connection_samples (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id         TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    received_at       TEXT NOT NULL,
    started_at_ms     INTEGER NOT NULL,
    ended_at_ms       INTEGER NOT NULL,
    trigger           TEXT NOT NULL,        -- PERIODIC | NETWORK_CHANGE | STOPPED
    network           TEXT NOT NULL,        -- WIFI | ETHERNET | CELLULAR | OTHER | NONE
    metered           INTEGER NOT NULL,
    vpn               INTEGER NOT NULL,
    at_home           INTEGER,              -- null: the phone couldn't tell
    response_ms       INTEGER,              -- median time to first response; null: none opened
    network_bytes     INTEGER NOT NULL,
    network_ms        INTEGER NOT NULL,
    throughput_kbps   INTEGER,              -- null: too little read to tell
    cached_bytes      INTEGER NOT NULL,
    downloaded_bytes  INTEGER NOT NULL,
    stalls            INTEGER NOT NULL,
    stall_ms          INTEGER NOT NULL,
    server_id         TEXT,
    profile_id        TEXT,                 -- adaptive.continuo: HLS
    codec             TEXT,
    max_bitrate_kbps  INTEGER,              -- HLS: the level playing
    reason            TEXT,
    capped            INTEGER NOT NULL,
    capped_ms         INTEGER NOT NULL
);
CREATE INDEX idx_connection_samples_device ON connection_samples(device_id, ended_at_ms);
CREATE INDEX idx_connection_samples_ended ON connection_samples(ended_at_ms);
