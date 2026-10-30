-- How many songs each release group has (MusicBrainz's canonical release), for telling a library
-- album that's complete from one the library only partly has: the apps' Albums can leave the
-- partial ones out (the album page then shows what's missing). Filled in the background, a few
-- albums at a time at MusicBrainz's pace. 0 = MusicBrainz had no tracklist.
CREATE TABLE album_track_counts (
    release_group_id TEXT PRIMARY KEY,
    track_count      INTEGER NOT NULL,
    checked_at       TEXT NOT NULL
);
