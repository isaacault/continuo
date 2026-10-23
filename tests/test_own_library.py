"""An account's own library (#27): what's in it, what's only on the server, Add, Remove, Undo,
and requests landing in the requester's library."""
import asyncio
import dataclasses
import os

import pytest
from conftest import LIDARR, bearer, login, make_user

from continuo import config
from continuo.db import account_libraries as acc_db
from continuo.db import cache as cache_db
from continuo.db import library as lib_db
from continuo.db import members as members_db
from continuo.db import requests as req_db
from continuo.library import links
from continuo.requests.normalise import album_key
from continuo.search import merge
from continuo.workers import scan

RG_GEO = "4a1b2c3d-0000-4000-8000-000000000001"
RG_MHTRTC = "4a1b2c3d-0000-4000-8000-000000000002"
GEO, MHTRTC = "Boards of Canada/Geogaddi (2002)", "Boards of Canada/Music Has the Right to Children (1998)"


@pytest.fixture
def server(tmp_path, monkeypatch, fake):
    """Two albums in the store; isaac's library (Navidrome library 2) has Geogaddi."""
    store, libs = tmp_path / "media" / "music", tmp_path / "media" / "libraries"
    for folder in (GEO, MHTRTC):
        (store / folder).mkdir(parents=True)
        (store / folder / "01.flac").write_bytes(b"x" * 100)
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        config.settings, music_dir=str(store), libraries_dir=str(libs)))
    lib_db.upsert_many([
        {"navidrome_id": "s-geo", "name": "Geogaddi", "artist": "Boards of Canada", "mbz_release_group_id": RG_GEO,
         "folder": GEO, "norm_key": album_key("Boards of Canada", "Geogaddi"), "library_id": 1, "song_count": 23},
        {"navidrome_id": "s-mh", "name": "Music Has the Right to Children", "artist": "Boards of Canada",
         "mbz_release_group_id": RG_MHTRTC, "folder": MHTRTC, "library_id": 1, "song_count": 18,
         "norm_key": album_key("Boards of Canada", "Music Has the Right to Children")},
        {"navidrome_id": "u-geo", "name": "Geogaddi", "artist": "Boards of Canada", "mbz_release_group_id": RG_GEO,
         "folder": GEO, "norm_key": album_key("Boards of Canada", "Geogaddi"), "library_id": 2, "song_count": 23},
    ])
    isaac = make_user("isaac", role="user")
    acc_db.set(isaac["id"], 2, "nd-isaac")
    members_db.add(isaac["id"], GEO, "migration")
    links.sync_user(isaac, *links.roots())
    fake.on("GET", f"{LIDARR}/album", [{"id": 5, "foreignAlbumId": RG_MHTRTC, "monitored": True, "artistId": 9}])
    fake.on("PUT", f"{LIDARR}/album/monitor", {}, status=202)
    fake.on("POST", f"{LIDARR}/command", {"id": 1})
    fake.on("GET", f"{LIDARR}/album/5", {"id": 5, "foreignAlbumId": RG_MHTRTC, "monitored": True, "artistId": 9})
    fake.on("GET", "http://navidrome.test/rest/startScan", {"subsonic-response": {"status": "ok", "scanStatus": {}}})
    return store, libs, isaac


def _cat(rg, title):
    return {"musicBrainzReleaseGroupId": rg, "title": title, "artist": "Boards of Canada", "releaseIds": []}


def test_the_account_sees_its_own_albums_and_the_rest_as_on_server(server):
    *_, isaac = server
    index = merge._LibraryIndex(lib_db.all_albums(), isaac["id"])
    out = merge.merge_albums([], [_cat(RG_GEO, "Geogaddi"), _cat(RG_MHTRTC, "Music Has the Right to Children")],
                             index, {}, 10)
    by_title = {a["title"]: a["availability"] for a in out}
    assert by_title["Geogaddi"] == {"state": "in_library", "navidromeAlbumId": "u-geo", "trackCount": 23}
    assert by_title["Music Has the Right to Children"] == {"state": "on_server", "storeAlbumId": "s-mh",
                                                           "trackCount": 18}


def test_an_account_without_its_own_library_sees_the_store(server):
    other = make_user("guest", role="user")
    index = merge._LibraryIndex(lib_db.all_albums(), other["id"])
    assert index.mine(lib_db.get("s-mh")) == "s-mh"


def test_add_links_the_album_into_the_library(client, server):
    store, libs, isaac = server
    s = login(client)
    assert "own_library" in client.get("/v1/me", headers=bearer(s)).json()["capabilities"]
    r = client.post("/v1/library/albums/s-mh/add", headers=bearer(s))
    assert r.status_code == 200, r.text
    assert members_db.has(isaac["id"], MHTRTC)
    assert os.stat(libs / "isaac" / MHTRTC / "01.flac").st_ino == os.stat(store / MHTRTC / "01.flac").st_ino


def test_removing_an_album_others_have_only_takes_this_account_s_links(client, server):
    store, libs, isaac = server
    guest = make_user("guest", role="user")
    members_db.add(guest["id"], GEO)
    s = login(client)
    r = client.post("/v1/library/albums/u-geo/remove", headers=bearer(s))
    assert r.status_code == 200, r.text
    assert not (libs / "isaac" / GEO).exists() and (store / GEO / "01.flac").exists()
    assert cache_db.list_items("removed") == [], "the store keeps it: guest still has it"
    listed = client.get("/v1/library/removed", headers=bearer(s)).json()
    assert [i["title"] for i in listed] == ["Geogaddi"]
    assert client.post(f"/v1/library/removed/{listed[0]['id']}/restore", headers=bearer(s)).status_code == 200
    assert (libs / "isaac" / GEO / "01.flac").exists() and members_db.has(isaac["id"], GEO)
    assert client.get("/v1/library/removed", headers=bearer(s)).json() == []


def test_removing_the_last_copy_caches_it_and_undo_brings_it_back(client, fake, server):
    store, libs, isaac = server
    members_db.add(isaac["id"], MHTRTC)
    links.sync_user(isaac, *links.roots())
    lib_db.upsert_many([{"navidrome_id": "u-mh", "name": "Music Has the Right to Children", "artist": "Boards of Canada",
                         "mbz_release_group_id": RG_MHTRTC, "folder": MHTRTC, "library_id": 2, "norm_key": "k"}])
    s = login(client)
    r = client.post("/v1/library/albums/u-mh/remove", headers=bearer(s))
    assert r.status_code == 200, r.text
    assert not (store / MHTRTC).exists() and not (libs / "isaac" / MHTRTC).exists()
    assert len(cache_db.list_items("removed")) == 1, "nobody has it: into the media cache, Lidarr unmonitored"
    r = client.post(f"/v1/library/removed/{r.json()['id']}/restore", headers=bearer(s))
    assert r.status_code == 200, r.text
    assert (store / MHTRTC / "01.flac").exists() and (libs / "isaac" / MHTRTC / "01.flac").exists()
    assert cache_db.list_items("removed") == []


def test_someone_else_s_removal_cant_be_restored(client, server):
    *_, isaac = server
    guest = make_user("guest", role="user")
    acc_db.set(guest["id"], 3, None)
    members_db.add(guest["id"], GEO)
    s = login(client)
    rid = client.post("/v1/library/albums/u-geo/remove", headers=bearer(s)).json()["id"]
    g = login(client, "guest")
    assert client.post(f"/v1/library/removed/{rid}/restore", headers=bearer(g)).status_code == 404


def test_requesting_an_album_on_the_server_adds_it(client, server):
    *_, isaac = server
    s = login(client)
    r = client.post("/v1/requests", headers=bearer(s), json={"musicBrainzReleaseGroupId": RG_MHTRTC})
    assert r.status_code == 409 and r.json()["error"]["code"] == "added_to_library"
    assert members_db.has(isaac["id"], MHTRTC)


def test_an_arrived_request_goes_into_the_requester_s_library(server, monkeypatch):
    store, libs, isaac = server
    row = req_db.create(mb_release_group_id=RG_MHTRTC, title="Music Has the Right to Children",
                        artist="Boards of Canada", year=1998, artwork_url=None, user_id=isaac["id"], note=None)
    row = req_db.update(row["id"], status="scanning")

    async def matched(_row):
        from continuo.requests.matching import Match
        return Match(strategy="release_group", navidrome_album_id="s-mh")
    monkeypatch.setattr(scan, "try_match", matched)
    monkeypatch.setattr(scan, "_observe_scans", lambda: _none())
    monkeypatch.setattr(scan, "_maybe_request_scan", lambda rows: _none())
    asyncio.run(scan.tick())
    assert members_db.has(isaac["id"], MHTRTC) and (libs / "isaac" / MHTRTC / "01.flac").exists()
    assert req_db.get(row["id"])["status"] == "scanning", "not available until Navidrome indexed it there"
    lib_db.upsert_many([{"navidrome_id": "u-mh", "name": "Music Has the Right to Children", "artist": "Boards of Canada",
                         "mbz_release_group_id": RG_MHTRTC, "folder": MHTRTC, "library_id": 2, "norm_key": "k"}])
    asyncio.run(scan.tick())
    got = req_db.get(row["id"])
    assert got["status"] == "available" and got["navidrome_album_id"] == "u-mh"


async def _none():
    return None
