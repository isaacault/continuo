-- Artist identity across names (Ichiko Aoba in the library, 青葉市子 in MusicBrainz).
-- The library's own name for a MusicBrainz artist, when they differ: search folds the catalogue
-- artist into the library one, and albums Lidarr imports for it are tagged with this name.
CREATE TABLE artist_names (
    mb_artist_id  TEXT PRIMARY KEY,
    library_name  TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- Untagged library albums matched to a MusicBrainz release group by title ("Windswept Adan" is
-- a release of アダンの風). mb_release_group_id NULL = looked up, nothing found (retried later).
CREATE TABLE album_matches (
    navidrome_id         TEXT PRIMARY KEY,
    mb_release_group_id  TEXT,
    matched_at           TEXT NOT NULL
);
