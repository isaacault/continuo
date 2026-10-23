-- Per-user libraries (#27): the index records each album's Navidrome library, so matching keeps
-- to the store (STORE_LIBRARY_ID) and an account's own copies can be found by folder. NULL = an
-- album indexed before this (the store, which was the only library).
ALTER TABLE library_albums ADD COLUMN library_id INTEGER;
CREATE INDEX library_albums_library ON library_albums (library_id, folder);

-- Each account's own Navidrome library.
CREATE TABLE account_libraries (
    user_id               INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    navidrome_library_id  INTEGER NOT NULL,
    navidrome_user_id     TEXT,
    created_at            TEXT NOT NULL
);
