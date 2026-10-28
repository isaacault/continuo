-- Where an account's Play now songs are scrobbled (ListenBrainz, Last.fm). Library songs are
-- scrobbled by Navidrome, which knows them; Play now songs it doesn't, so continuo does. The
-- secret is the account's ListenBrainz token or Last.fm session key; never logged or returned.
CREATE TABLE scrobblers (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    service     TEXT NOT NULL,              -- listenbrainz | lastfm
    secret      TEXT NOT NULL,
    account     TEXT,                       -- the user name on that service
    created_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, service)
);
