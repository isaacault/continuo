"""Ported from lidseeker's test_monitoring.py / test_search_in_library.py, against the real
client code with a mocked Lidarr."""
import asyncio
from unittest.mock import patch

from conftest import LIDARR, run

from continuo.requests import orchestrate
from continuo.upstream import lidarr


def test_monitor_album_only_touches_target(fake):
    seen = []
    fake.on("PUT", f"{LIDARR}/album/monitor", handler=lambda r: seen.append(fake.body(r)) or {})
    fake.on("POST", f"{LIDARR}/command", {"id": None})
    fake.on("GET", f"{LIDARR}/album/99", {"id": 99, "monitored": True,
                                         "statistics": {"trackCount": 12}})
    assert run(lidarr.monitor_album(artist_id=42, album_id=99)) is True
    assert seen == [{"albumIds": [99], "monitored": True}] * 2
    refresh = fake.called("POST", f"{LIDARR}/command")
    assert fake.body(refresh[0]) == {"name": "RefreshArtist", "artistId": 42}


def test_monitor_album_false_without_tracks_or_monitor(fake):
    fake.on("PUT", f"{LIDARR}/album/monitor", {})
    fake.on("POST", f"{LIDARR}/command", {"id": None})
    fake.on("GET", f"{LIDARR}/album/99", {"monitored": True, "statistics": {"trackCount": 0}})
    assert run(lidarr.monitor_album(42, 99)) is False
    fake.on("GET", f"{LIDARR}/album/99", {"monitored": False, "statistics": {"trackCount": 9}})
    assert run(lidarr.monitor_album(42, 99)) is False


def test_albums_for_artist_paginates(fake):
    pages = {1: [{"id": i} for i in range(100)], 2: [{"id": 100}, {"id": 101}]}
    fake.on("GET", f"{LIDARR}/album",
            handler=lambda r: pages[int(r.url.params["page"])])
    albums = run(lidarr.albums_for_artist_id(1))
    assert len(albums) == 102
    assert [c.url.params["page"] for c in fake.calls] == ["1", "2"]


def test_add_artist_never_follows(fake):
    fake.on("POST", f"{LIDARR}/artist", handler=lambda r: {"id": 7, **fake.body(r)})
    created = run(lidarr.add_artist({"artistName": "X", "foreignArtistId": "a-1",
                                     "monitorNewItems": "all"}))
    assert created["monitorNewItems"] == "none"
    assert created["addOptions"] == {"monitor": "none", "searchForMissingAlbums": False}


def test_find_library_artist_uses_mbid_filter(fake):
    fake.on("GET", f"{LIDARR}/artist", [{"id": 3, "foreignArtistId": "a-1"}])
    assert run(lidarr.find_library_artist("a-1"))["id"] == 3
    assert fake.calls[0].url.params["mbId"] == "a-1"


def test_set_artist_monitored_pins_monitor_new_items(fake):
    fake.on("GET", f"{LIDARR}/artist/5", {"id": 5, "monitored": True, "monitorNewItems": "all"})
    put = []
    fake.on("PUT", f"{LIDARR}/artist/5", handler=lambda r: put.append(fake.body(r)) or {})
    run(lidarr.set_artist_monitored(5, True))
    assert put[0]["monitorNewItems"] == "none" and put[0]["monitored"] is True


def _patches(calls, *, existing):
    async def lookup(_fid):
        return {"foreignAlbumId": "alb-1", "title": "Test Album", "images": [],
                "artist": {"artistName": "Test Artist", "foreignArtistId": "art-1"}}

    async def find(_fid):
        return {"id": 10} if existing else None

    async def add(_a):
        calls["add_artist"] = True
        return {"id": 20}

    async def set_mon(aid, mon):
        calls["set_artist_monitored"] = (aid, mon)

    async def wait(aid, fid):
        return {"id": 99, "releases": [{"foreignReleaseId": "r1"}], "statistics": {}}

    async def monitor(aid, alb):
        calls["monitor_album"] = (aid, alb)
        return True

    async def get_album(aid, fresh=False):
        return {"id": aid, "releases": [{"foreignReleaseId": "r1", "monitored": True}]}

    async def search(ids):
        calls["album_search"] = ids

    return [patch.object(lidarr, "lookup_album_by_id", lookup),
            patch.object(lidarr, "find_library_artist", find),
            patch.object(lidarr, "add_artist", add),
            patch.object(lidarr, "set_artist_monitored", set_mon),
            patch.object(lidarr, "wait_for_album", wait),
            patch.object(lidarr, "monitor_album", monitor),
            patch.object(lidarr, "get_album", get_album),
            patch.object(lidarr, "album_search", search)]


def test_request_album_existing_artist(fake):
    calls = {}
    ps = _patches(calls, existing=True)
    for p in ps:
        p.start()
    try:
        res = asyncio.run(orchestrate.request_album("alb-1"))
    finally:
        for p in ps:
            p.stop()
    assert res["lidarr_artist_id"] == 10 and res["lidarr_album_id"] == 99
    assert res["lidarr_release_id"] == "r1"
    assert calls["set_artist_monitored"] == (10, True)
    assert "add_artist" not in calls
    assert calls["monitor_album"] == (10, 99) and calls["album_search"] == [99]


def test_request_album_new_artist_is_switched_on(fake):
    calls = {}
    ps = _patches(calls, existing=False)
    for p in ps:
        p.start()
    try:
        res = asyncio.run(orchestrate.request_album("alb-1"))
    finally:
        for p in ps:
            p.stop()
    # Lidarr 3 leaves a monitor=none add unmonitored: request_album must switch it on.
    assert calls["add_artist"] and calls["set_artist_monitored"] == (20, True)
    assert res["lidarr_artist_id"] == 20 and calls["monitor_album"] == (20, 99)


def test_ensure_album_monitored_sets_artist_on_first(fake):
    order = []
    fake.on("GET", f"{LIDARR}/album/88", {"monitored": False,
                                         "statistics": {"trackCount": 0, "trackFileCount": 0}})

    async def set_mon(aid, mon):
        order.append(("artist", aid, mon))

    async def monitor(aid, alb):
        order.append(("album", aid, alb))
        return True

    with patch.object(lidarr, "set_artist_monitored", set_mon), \
            patch.object(lidarr, "monitor_album", monitor):
        assert run(lidarr.ensure_album_monitored(5, 88)) is True
    assert order == [("artist", 5, True), ("album", 5, 88)]


def test_ensure_album_monitored_noop_when_downloaded(fake):
    fake.on("GET", f"{LIDARR}/album/88", {"monitored": False,
                                         "statistics": {"trackCount": 9, "trackFileCount": 3}})
    assert run(lidarr.ensure_album_monitored(5, 88)) is True
    assert len(fake.calls) == 1


# --- fix/88: search must not mark a library artist's whole discography as owned -----------
LOOKUP = [
    {"id": 1, "foreignAlbumId": "owned", "title": "Owned"},
    {"id": 2, "foreignAlbumId": "known", "title": "Known, not downloaded"},
    {"foreignAlbumId": "new", "title": "Not in Lidarr"},
]
LIBRARY = [{"id": 1, "statistics": {"trackFileCount": 12}},
           {"id": 2, "statistics": {"trackFileCount": 0}}]


def test_album_search_only_marks_downloaded(fake):
    fake.on("GET", f"{LIDARR}/album/lookup", LOOKUP)
    fake.on("GET", f"{LIDARR}/album", handler=lambda r: [
        a for a in LIBRARY if str(a["id"]) in r.url.params.get_list("albumIds")])
    got = {a["musicBrainzReleaseGroupId"]: a["owned"] for a in run(lidarr.search_albums("x"))}
    assert got == {"owned": True, "known": False, "new": False}
    assert fake.calls[1].url.params.get_list("albumIds") == ["1", "2"]


def test_album_search_stats_failure_leaves_requestable(fake):
    fake.on("GET", f"{LIDARR}/album/lookup", LOOKUP)
    fake.on("GET", f"{LIDARR}/album", {}, status=500)
    assert not any(a["owned"] for a in run(lidarr.search_albums("x")))


def test_normalize_album_bare_id_not_owned():
    assert lidarr.normalize_album({"id": 5})["owned"] is False
    assert lidarr.normalize_album({"id": 5, "statistics": {"trackFileCount": 3}})["owned"] is True


def test_reconcile_records_artist_drift(fake):
    from continuo.db import requests as req_db
    from continuo.workers import reconcile
    row = req_db.create(mb_release_group_id="rg-r", title="Honeycomb", artist="Jitwam", year=2019,
                        artwork_url=None, user_id=None)
    req_db.update(row["id"], status="searching", lidarr_album_id=556, lidarr_artist_id=49)
    fake.on("GET", f"{LIDARR}/album/556", {"id": 556, "monitored": True, "statistics": {
        "trackCount": 12, "trackFileCount": 0}})
    artist = {"id": 49, "monitored": False, "monitorNewItems": "none"}
    fake.on("GET", f"{LIDARR}/artist/49", handler=lambda r: dict(artist))
    fake.on("PUT", f"{LIDARR}/artist/49", handler=lambda r: artist.update(fake.body(r)) or {})
    fake.on("PUT", f"{LIDARR}/album/monitor", {})
    fake.on("POST", f"{LIDARR}/command", {"id": None})
    run(reconcile.tick())
    assert artist["monitored"] is True and artist["monitorNewItems"] == "none"
    ev = [e for e in req_db.events(row["id"]) if e["type"] == "remonitored"]
    assert ev and ev[0]["detail"]["drift"] == ["artist_unmonitored"]
    run(reconcile.tick())          # stable now: no second event
    assert len([e for e in req_db.events(row["id"]) if e["type"] == "remonitored"]) == 1
