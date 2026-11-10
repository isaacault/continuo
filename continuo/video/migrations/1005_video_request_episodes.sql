-- A series request can name episodes, not only whole seasons: JSON [{"season": 4, "episodes": [3, 4],
-- "onward": true}] (onward: and the season's later episodes as they air). future: new seasons too
-- (null: yes, as before).
ALTER TABLE video_requests ADD COLUMN episodes TEXT;
ALTER TABLE video_requests ADD COLUMN future INTEGER;
