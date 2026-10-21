-- Latency milestones per request, to measure what "play now" could achieve (issue #10).
-- created_at (requested) and available_at already exist.
ALTER TABLE requests ADD COLUMN download_started_at TEXT;   -- first slskd transfer or Lidarr grab
ALTER TABLE requests ADD COLUMN first_file_at TEXT;         -- first complete audio file anywhere
ALTER TABLE requests ADD COLUMN first_source TEXT;          -- soulseek | torrent: who delivered first

-- Backfill from the event log: the first move to "downloading", and the first move past it.
UPDATE requests SET download_started_at = (
    SELECT MIN(e.at) FROM request_events e
    WHERE e.request_id = requests.id AND e.type IN ('status', 'webhook_grab')
      AND (e.type = 'webhook_grab' OR json_extract(e.detail, '$.to') = 'downloading')
);
UPDATE requests SET first_file_at = (
    SELECT MIN(e.at) FROM request_events e
    WHERE e.request_id = requests.id AND e.type = 'status'
      AND json_extract(e.detail, '$.to') IN ('importing', 'scanning', 'available')
)
WHERE first_file_at IS NULL;
UPDATE requests SET first_source = last_source WHERE first_source IS NULL;
