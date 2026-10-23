-- Per-user libraries (#27): what each account removed from its own library, for Undo and
-- "Recently removed". cache_item_id is set when it was the last copy, which went to the media
-- cache (#23); otherwise other libraries still have it and only this account's links went.
CREATE TABLE library_removals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    folder              TEXT NOT NULL,
    title               TEXT,
    artist              TEXT,
    mb_release_group_id TEXT,
    navidrome_album_id  TEXT,
    cache_item_id       INTEGER,
    removed_at          TEXT NOT NULL
);
CREATE INDEX library_removals_user ON library_removals (user_id, removed_at);
