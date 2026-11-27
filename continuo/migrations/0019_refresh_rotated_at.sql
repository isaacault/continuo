-- When each device's refresh token was last rotated (#129), so a retry of a refresh whose reply
-- was lost (the previous token, presented again within REFRESH_REUSE_GRACE_SECONDS) can be told
-- apart from a stolen token coming back later. NULL until the device's next refresh.
ALTER TABLE devices ADD COLUMN refresh_rotated_at TEXT;
