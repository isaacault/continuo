-- Play now: when each was last started or watched (the latest gets the bandwidth; older ones carry on
-- slowly into the library), and, for a series, which episode.
ALTER TABLE video_requests ADD COLUMN play_now_at TEXT;
ALTER TABLE video_requests ADD COLUMN play_now_season INTEGER;
ALTER TABLE video_requests ADD COLUMN play_now_episode INTEGER;
