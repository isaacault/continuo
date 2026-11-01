-- My List and ratings (the Netflix-like apps): per account, any title, in the library or not.
-- What a title is (its name, year, pictures) is kept with the entry so the list shows without
-- asking TMDB for every title.
CREATE TABLE video_my_list (
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('movie', 'series')),
    tmdb_id       INTEGER NOT NULL,
    title         TEXT NOT NULL,
    year          INTEGER,
    poster_path   TEXT,
    backdrop_path TEXT,
    added_at      TEXT NOT NULL,
    PRIMARY KEY (user_id, kind, tmdb_id)
);
-- -1 not for me, 1 like, 2 love (Netflix's thumbs); no row: not rated.
CREATE TABLE video_ratings (
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind     TEXT NOT NULL CHECK (kind IN ('movie', 'series')),
    tmdb_id  INTEGER NOT NULL,
    rating   INTEGER NOT NULL CHECK (rating IN (-1, 1, 2)),
    title    TEXT NOT NULL,
    rated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, kind, tmdb_id)
);
