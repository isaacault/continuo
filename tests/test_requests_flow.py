"""The whole loop against mocked upstreams: request → Lidarr add → searching → webhook grab →
import → scanning → matched in Navidrome → available + notification (inbox, HA)."""
import time

from conftest import LIDARR, ND, bearer, login, make_user, run

from continuo import config
from continuo.db import library as lib_db
from continuo.db import notifications as notif_db
from continuo.db import requests as req_db
from continuo.requests import orchestrate
from continuo.upstream import http
from continuo.workers import scan

RG = "4a1b2c3d-0000-4000-8000-000000000001"
HOOK = {"X-Webhook-Secret": "hook-secret"}


def lidarr_world(fake, state):
    """A stateful fake Lidarr for one album request."""
    album = {"id": 412, "foreignAlbumId": RG, "title": "Geogaddi", "releaseDate": "2002-02-18",
             "artist": {"artistName": "Boards of Canada", "foreignArtistId": "art-boc"},
             "images": [{"coverType": "cover", "remoteUrl": "https://img/geo.jpg"}],
             "releases": [{"foreignReleaseId": "rel-1", "monitored": True, "trackCount": 23}]}
    state.setdefault("monitored", False)
    state.setdefault("files", 0)

    def album_row(_r=None):
        return {**album, "monitored": state["monitored"],
                "statistics": {"trackCount": 23, "trackFileCount": state["files"],
                               "percentOfTracks": 100 * state["files"] / 23}}

    def monitor(r):
        state["monitored"] = fake.body(r)["monitored"]
        return {}

    fake.on("GET", f"{LIDARR}/album/lookup", [album])
    fake.on("GET", f"{LIDARR}/artist", [])
    fake.on("POST", f"{LIDARR}/artist", handler=lambda r: state.update(added=fake.body(r)) or
            {"id": 9, **fake.body(r)})
    fake.on("GET", f"{LIDARR}/album", handler=lambda r: [album_row()])
    fake.on("GET", f"{LIDARR}/album/412", handler=album_row)
    fake.on("PUT", f"{LIDARR}/album/monitor", handler=monitor)
    fake.on("POST", f"{LIDARR}/command", handler=lambda r: state.setdefault(
        "commands", []).append(fake.body(r)) or {"id": None})
    fake.on("GET", f"{LIDARR}/queue", {"records": [], "totalRecords": 0})
    fake.on("GET", f"{LIDARR}/trackfile", [
        {"path": "/data/music/Boards of Canada/Geogaddi (2002)/01 - Ready Lets Go.flac"}])


def test_full_request_lifecycle(client, fake, monkeypatch):
    env = dict(config.settings.__dict__, ha_webhook_url="http://ha.test/api/webhook/music")
    monkeypatch.setattr(config, "settings", config.settings.__class__(**env))
    http.reset()
    ha = []
    fake.on("POST", "http://ha.test/api/webhook/music", handler=lambda r: ha.append(fake.body(r))
            or {})
    make_user()
    s = login(client)
    state = {}
    lidarr_world(fake, state)

    r = client.post("/v1/requests", headers=bearer(s),
                    json={"kind": "album", "musicBrainzReleaseGroupId": RG})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["title"] == "Geogaddi" and r.json()["status"] == "pending"

    # The add runs as a background task on the app's loop; wait for it.
    for _ in range(100):
        if req_db.get(rid)["status"] != "pending":
            break
        time.sleep(0.05)
    row = req_db.get(rid)
    assert row["status"] == "searching", row
    assert row["lidarr_album_id"] == 412 and row["lidarr_release_id"] == "rel-1"
    assert state["added"]["monitorNewItems"] == "none"
    assert state["added"]["addOptions"] == {"monitor": "none", "searchForMissingAlbums": False}
    assert {"name": "AlbumSearch", "albumIds": [412]} in state["commands"]

    # Same album again → 409 with the live request id.
    again = client.post("/v1/requests", headers=bearer(s),
                        json={"musicBrainzReleaseGroupId": RG})
    assert again.status_code == 409
    assert again.json()["error"] == {"code": "already_requested", "requestId": rid,
                                     "message": "That album is already requested.",
                                     "retryable": False}

    # Lidarr grabs, then imports.
    assert client.post("/internal/webhooks/lidarr", headers=HOOK, json={
        "eventType": "Grab", "albums": [{"id": 412, "mbId": RG}], "downloadId": "abc",
        "downloadClient": "qBittorrent", "downloadClientType": "qBittorrent",
        "release": {"releaseTitle": "BoC - Geogaddi FLAC"}}).status_code == 200
    assert req_db.get(rid)["status"] == "downloading"
    state["files"] = 23
    r = client.post("/internal/webhooks/lidarr", headers=HOOK, json={
        "eventType": "Download", "isUpgrade": False,
        "album": {"id": 412, "mbId": RG, "title": "Geogaddi"},
        "trackFiles": [{"path": "/data/music/Boards of Canada/Geogaddi (2002)/01 - Ready Lets Go.flac"}]})
    assert r.json()["requests"] == [rid]
    assert req_db.get(rid)["status"] == "scanning"

    # Navidrome has it (indexed); the scan watcher matches by path.
    lib_db.upsert_many([{"navidrome_id": "nd-geo", "name": "Geogaddi", "artist": "Boards of Canada",
                         "year": 2002, "song_count": 23, "mbz_album_id": None,
                         "mbz_release_group_id": None, "folder": "Boards of Canada/Geogaddi (2002)",
                         "norm_key": "x", "source_updated_at": None}])
    fake.on("GET", f"{ND}/rest/getScanStatus", {"subsonic-response": {
        "status": "ok", "scanStatus": {"scanning": False, "lastScan": "t1"}}})
    run(scan.tick())
    row = req_db.get(rid)
    assert row["status"] == "available" and row["navidrome_album_id"] == "nd-geo"
    events = req_db.events(rid)
    matched = [e for e in events if e["type"] == "matched"]
    assert matched[0]["detail"]["strategy"] == "paths"

    out = client.get(f"/v1/requests/{rid}", headers=bearer(s)).json()
    assert out["play"] == {"navidromeAlbumId": "nd-geo",
                           "deepLink": "polyphony://album/music-api.test/nd-geo"}
    inbox = client.get("/v1/notifications", headers=bearer(s)).json()
    assert inbox["unreadCount"] == 1
    n = inbox["items"][0]
    assert n["type"] == "request_available" and n["body"] == "Geogaddi — Boards of Canada"
    assert n["action"]["deepLink"] == "polyphony://album/music-api.test/nd-geo"
    assert ha == [{"title": "Ready to play", "message": "Geogaddi — Boards of Canada",
                   "tag": f"request-{rid}", "kind": "request_available",
                   "clickAction": "polyphony://album/music-api.test/nd-geo", "username": "isaac"}]
    assert client.post("/v1/notifications/read", headers=bearer(s),
                       json={"all": True}).json()["unreadCount"] == 0


def test_lidarr_add_failure_is_retryable_error(client, fake):
    make_user()
    s = login(client)
    fake.on("GET", f"{LIDARR}/album/lookup", [{"foreignAlbumId": RG, "title": "VA Comp",
                                              "artist": {"artistName": "Various",
                                                         "foreignArtistId": "va"}}])
    fake.on("GET", f"{LIDARR}/artist", [])
    fake.on("POST", f"{LIDARR}/artist", {"id": 3})
    fake.on("GET", f"{LIDARR}/album", [])
    monkey = orchestrate.lidarr

    async def no_album(*_a, **_k):
        return None
    orig = monkey.wait_for_album
    monkey.wait_for_album = no_album
    try:
        rid = client.post("/v1/requests", headers=bearer(s),
                          json={"musicBrainzReleaseGroupId": RG}).json()["id"]
        for _ in range(100):
            if req_db.get(rid)["status"] == "error":
                break
            time.sleep(0.05)
    finally:
        monkey.wait_for_album = orig
    out = client.get(f"/v1/requests/{rid}", headers=bearer(s)).json()
    assert out["status"] == "error" and out["error"]["retryable"] is True
    assert "compilation" in out["error"]["message"]
    assert notif_db.list_for_user(1)[0]["type"] == "request_error"


def test_in_library_conflict(client, fake):
    make_user()
    s = login(client)
    lib_db.upsert_many([{"navidrome_id": "nd-1", "name": "Geogaddi", "artist": "BoC", "year": None,
                         "song_count": 23, "mbz_album_id": None, "mbz_release_group_id": RG,
                         "folder": None, "norm_key": "x", "source_updated_at": None}])
    r = client.post("/v1/requests", headers=bearer(s), json={"musicBrainzReleaseGroupId": RG})
    assert r.status_code == 409 and r.json()["error"]["navidromeAlbumId"] == "nd-1"


def test_users_only_see_their_own_requests(client, fake):
    make_user()
    make_user("charlie", role="user")
    admin, charlie = login(client), login(client, "charlie")
    row = req_db.create(mb_release_group_id=RG, title="T", artist="A", year=None,
                        artwork_url=None, user_id=1)
    req_db.update(row["id"], status="available")
    assert client.get(f"/v1/requests/{row['id']}", headers=bearer(charlie)).status_code == 404
    assert client.get("/v1/requests", headers=bearer(charlie)).json()["items"] == []
    items = client.get("/v1/requests", headers=bearer(admin)).json()["items"]
    assert items[0]["requestedBy"] == "isaac"


def test_delete_unmonitors(client, fake):
    make_user()
    s = login(client)
    row = req_db.create(mb_release_group_id=RG, title="T", artist="A", year=None,
                        artwork_url=None, user_id=1)
    req_db.update(row["id"], status="searching", lidarr_album_id=412, lidarr_artist_id=9)
    put = []
    fake.on("PUT", f"{LIDARR}/album/monitor", handler=lambda r: put.append(fake.body(r)) or {})
    assert client.delete(f"/v1/requests/{row['id']}", headers=bearer(s)).status_code == 200
    assert put == [{"albumIds": [412], "monitored": False}]
    assert req_db.get(row["id"]) is None


def test_webhook_secret_required(client):
    assert client.post("/internal/webhooks/lidarr", json={"eventType": "Test"}).status_code == 401
    r = client.post("/internal/webhooks/lidarr?secret=hook-secret", json={"eventType": "Test"})
    assert r.json() == {"ok": True, "eventType": "Test"}


def test_upgrade_import_raises_attention(client, fake):
    make_user()
    row = req_db.create(mb_release_group_id=RG, title="Geogaddi", artist="BoC", year=None,
                        artwork_url=None, user_id=1)
    req_db.update(row["id"], status="downloading", lidarr_album_id=412)
    client.post("/internal/webhooks/lidarr", headers=HOOK, json={
        "eventType": "Download", "isUpgrade": True, "album": {"id": 412, "mbId": RG}})
    got = req_db.get(row["id"])
    assert got["status"] == "needs_attention"
    assert got["attention"]["reason"] == "upgrade_detected"


def test_scan_timeout_then_recovery(fake, monkeypatch):
    env = dict(config.settings.__dict__, scan_timeout_seconds=0, scan_watcher_grace_seconds=0)
    monkeypatch.setattr(config, "settings", config.settings.__class__(**env))
    make_user()
    row = req_db.create(mb_release_group_id=RG, title="Geogaddi", artist="BoC", year=2002,
                        artwork_url=None, user_id=1)
    req_db.update(row["id"], status="scanning", lidarr_album_id=412,
                  scan_started_at="2026-01-01T00:00:00+00:00",
                  import_paths=["/data/music/BoC/Geogaddi/01.flac"])
    fake.on("GET", f"{LIDARR}/album/412", {"id": 412, "releases": [], "statistics": {}})
    fake.on("GET", f"{ND}/rest/getScanStatus", {"subsonic-response": {
        "status": "ok", "scanStatus": {"scanning": False, "lastScan": "t1"}}})
    fake.on("GET", f"{ND}/rest/startScan", {"subsonic-response": {"status": "ok",
                                                                  "scanStatus": {}}})
    fake.on("GET", f"{ND}/api/album", [], headers={"x-total-count": "0"})
    run(scan.tick())
    got = req_db.get(row["id"])
    assert got["status"] == "needs_attention"
    assert got["attention"]["reason"] == "import_not_visible"
    assert fake.called("GET", f"{ND}/rest/startScan")
    # The album shows up on a later scan → recovers to available.
    lib_db.upsert_many([{"navidrome_id": "nd-geo", "name": "Geogaddi", "artist": "BoC",
                         "year": 2002, "song_count": 23, "mbz_album_id": None,
                         "mbz_release_group_id": None, "folder": "BoC/Geogaddi",
                         "norm_key": "x", "source_updated_at": None}])
    run(scan.tick())
    assert req_db.get(row["id"])["status"] == "available"


def test_search_report_from_torrent_cleanup(client):
    row = req_db.create(mb_release_group_id=RG, title="T", artist="A", year=None,
                        artwork_url=None, user_id=None)
    req_db.update(row["id"], lidarr_album_id=412)
    r = client.post("/internal/events/search", headers=HOOK,
                    json={"lidarrAlbumId": 412, "at": "2026-10-20T13:00:00+00:00", "rule": "10"})
    assert r.json()["requestId"] == row["id"]
    assert req_db.get(row["id"])["last_search_at"] == "2026-10-20T13:00:00+00:00"
