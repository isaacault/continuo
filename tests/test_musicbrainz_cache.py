"""MusicBrainz answers kept on disk: fresh ones instead of asking, stale ones when it can't be asked."""
import asyncio

import httpx
import pytest

from continuo.db import musicbrainz_cache as cache_db
from continuo.db.conn import connect
from continuo.upstream import musicbrainz

MB = "https://musicbrainz.org/ws/2"
RG = "22222222-0000-4000-8000-000000000002"


@pytest.fixture
def mb(fake, monkeypatch):
    monkeypatch.setattr(musicbrainz, "_RETRY_WAITS", (0.0, 0.0))
    monkeypatch.setattr(musicbrainz, "_MIN_INTERVAL", 0.0)
    state = {"status": 200, "calls": 0}

    def answer(req):
        state["calls"] += 1
        if state["status"] != 200:
            return httpx.Response(state["status"])
        return httpx.Response(200, json={"title": "Being So Normal", "releases": [], "calls": state["calls"]})
    fake.on("GET", f"{MB}/release-group/{RG}", handler=answer)
    return state


def _age(days: float) -> None:
    with connect() as c:
        c.execute("UPDATE musicbrainz_cache SET fetched_at=datetime('now', ?)", (f"-{days} days",))


def test_a_fresh_answer_is_used_without_asking(mb):
    first = asyncio.run(musicbrainz._get(f"/release-group/{RG}", {"inc": "releases"}))
    again = asyncio.run(musicbrainz._get(f"/release-group/{RG}", {"inc": "releases"}))
    assert first == again and mb["calls"] == 1


def test_a_stale_answer_is_asked_again(mb):
    asyncio.run(musicbrainz._get(f"/release-group/{RG}", {"inc": "releases"}))
    _age(8)                                           # release groups are fresh for 7 days
    out = asyncio.run(musicbrainz._get(f"/release-group/{RG}", {"inc": "releases"}))
    assert mb["calls"] == 2 and out["calls"] == 2


def test_a_busy_musicbrainz_gets_the_stale_answer(mb):
    asyncio.run(musicbrainz._get(f"/release-group/{RG}", {"inc": "releases"}))
    _age(30)
    mb["status"] = 503
    out = asyncio.run(musicbrainz._get(f"/release-group/{RG}", {"inc": "releases"}))
    assert out["title"] == "Being So Normal", "an old answer beats none while MusicBrainz is down"


def test_nothing_cached_and_busy_still_fails(mb):
    mb["status"] = 503
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(musicbrainz._get(f"/release-group/{RG}", {"inc": "releases"}))


def test_not_found_is_never_cached_or_covered(mb):
    mb["status"] = 404
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(musicbrainz._get(f"/release-group/{RG}", {"inc": "releases"}))
    assert cache_db.total_bytes() == 0


def test_the_cache_drops_the_least_recently_used_beyond_its_size():
    for i in range(10):
        cache_db.put(f"/release/{i}", {"pad": "x" * 1000})
    with connect() as c:
        c.execute("UPDATE musicbrainz_cache SET used_at='2026-01-01T00:00:00+00:00' WHERE key='/release/0'")
    assert cache_db.prune(5000) > 0
    assert cache_db.get("/release/0") is None and cache_db.get("/release/9") is not None
    assert cache_db.total_bytes() <= 5000
