-- Latin names for non-Latin artists (NAME_LOCALE): the original name and the sort name next to
-- the library's name, so the app can show both and search can match either.
ALTER TABLE artist_names ADD COLUMN original_name TEXT;
ALTER TABLE artist_names ADD COLUMN sort_name TEXT;
