"""Lyrics from LRCLIB for songs the library has none for, and Play now songs."""
from conftest import bearer, login, make_user

from continuo.upstream import lrclib

LRCLIB = "https://lrclib.net/api/get"


def test_lrc_becomes_timed_lines_in_order():
    lines = lrclib.parse_lrc("[00:12.50] second\n[00:01.2]first\nnot a line\n[01:02:345] third")
    assert lines == [{"startMs": 1200, "text": "first"}, {"startMs": 12500, "text": "second"},
                     {"startMs": 62345, "text": "third"}]


def test_synced_lyrics_are_served_and_kept(client, fake):
    make_user()
    s = login(client)
    fake.on("GET", LRCLIB, {"syncedLyrics": "[00:01.00] I'm working late", "plainLyrics": "I'm working late"})
    params = {"artist": "Sabrina Carpenter", "title": "Espresso", "album": "Short n' Sweet", "durationMs": 175_000}
    r = client.get("/v1/library/lyrics", params=params, headers=bearer(s))
    assert r.status_code == 200, r.text
    assert r.json() == {"synced": True, "lines": [{"startMs": 1000, "text": "I'm working late"}], "instrumental": False, "source": "lrclib"}
    asked = fake.called("GET", LRCLIB)
    assert asked[0].url.params["duration"] == "175"
    client.get("/v1/library/lyrics", params=params, headers=bearer(s))
    assert len(fake.called("GET", LRCLIB)) == 1, "the second time from the cache"


def test_without_the_album_when_the_album_doesn_t_match_then_none(client, fake):
    make_user()
    s = login(client)
    seen = []

    def handler(req):
        import httpx
        seen.append(dict(req.url.params))
        return httpx.Response(404, json={"code": 404})
    fake.on("GET", LRCLIB, handler=handler)
    r = client.get("/v1/library/lyrics", params={"artist": "Nobody", "title": "Nothing", "album": "X"}, headers=bearer(s))
    assert r.status_code == 404
    assert [("album_name" in p) for p in seen] == [True, False]
    client.get("/v1/library/lyrics", params={"artist": "Nobody", "title": "Nothing", "album": "X"}, headers=bearer(s))
    assert len(seen) == 2, "none is remembered too"
