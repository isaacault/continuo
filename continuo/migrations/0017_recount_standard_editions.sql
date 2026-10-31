-- Albums are now measured against their standard edition (the official release with the fewest
-- songs), not the earliest release, which could be a deluxe one: a deluxe edition's bonus tracks no
-- longer make an album partial. Counted again in the background (MusicBrainz answers are cached).
DELETE FROM album_track_counts;
