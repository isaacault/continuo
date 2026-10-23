-- Per-user libraries (#27): which album folders (relative to the store, MUSIC_DIR) each account
-- has. The source of truth for "does anyone have this album"; hardlink counts are not (the
-- Soulseek share and seeding torrents hold links too).
CREATE TABLE library_members (
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    folder    TEXT NOT NULL,
    origin    TEXT NOT NULL DEFAULT 'add',     -- add | request | migration
    added_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, folder)
);
CREATE INDEX library_members_folder ON library_members (folder);
