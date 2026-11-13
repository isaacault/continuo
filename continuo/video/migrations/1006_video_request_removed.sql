-- Episodes deleted from the library on purpose (watched, say): JSON [[season, episode], ...]. A
-- change to the request never fetches them again.
ALTER TABLE video_requests ADD COLUMN removed TEXT;
