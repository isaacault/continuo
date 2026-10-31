-- Each continuo device's own Jellyfin token (docs/design/video.md §7.1): issued by signing the
-- account's Jellyfin user in with continuo's device ID, revoked by deleting that Jellyfin device
-- when the continuo device is signed out. The account's Jellyfin password is what issues them:
-- random for users continuo created, the user's own for ones an admin linked.
ALTER TABLE video_accounts ADD COLUMN jellyfin_password TEXT;
ALTER TABLE video_accounts ADD COLUMN managed INTEGER NOT NULL DEFAULT 1;   -- 1: continuo created it
CREATE TABLE video_device_tokens (
    device_id  TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
