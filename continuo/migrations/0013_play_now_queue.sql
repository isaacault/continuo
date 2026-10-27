-- Play now in a queue (polyphony#164): several queued songs are fetched ahead, a few at a time,
-- the one needed soonest first. priority: lower plays sooner (the app sends its queue's order;
-- a song tapped to play now goes first). A stream waits as 'queued' until its turn.
ALTER TABLE streams ADD COLUMN priority INTEGER NOT NULL DEFAULT 1000000;
CREATE INDEX streams_queue ON streams (user_id, status, priority);
