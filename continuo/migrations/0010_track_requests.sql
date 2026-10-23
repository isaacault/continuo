-- Single-song requests (#24). A track request is a requests row with kind = 'track' whose
-- mb_release_group_id holds the song's MusicBrainz *recording* id (the row's unique key, one
-- request per song); its album is album_release_group_id. Lidarr only deals in albums, so a
-- track comes from Soulseek directly: track holds what filing it needs (album, position, tags),
-- fetch the Soulseek copy being downloaded and the ones that failed.
ALTER TABLE requests ADD COLUMN album_release_group_id TEXT;
ALTER TABLE requests ADD COLUMN track TEXT;              -- JSON
ALTER TABLE requests ADD COLUMN fetch TEXT;              -- JSON
ALTER TABLE requests ADD COLUMN next_attempt_at TEXT;
ALTER TABLE requests ADD COLUMN file_path TEXT;          -- where it was filed, relative to MUSIC_DIR
CREATE INDEX idx_requests_kind ON requests(kind, status);
