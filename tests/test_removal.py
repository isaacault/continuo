"""Removing albums from the library into the media cache, restoring them, and eviction (#23)."""
import dataclasses
import os
from datetime import datetime, timedelta, timezone

import pytest
from conftest import LIDARR, bearer, login, make_user

from continuo import config
from continuo.db import cache as cache_db
from continuo.db import library as lib_db
from continuo.db.conn import connect
from continuo.library import cache
from continuo.requests.normalise import album_key

RG = "4a1b2c3d-0000-4000-8000-000000000001"


@pytest.fixture
def music(tmp_path, monkeypatch, fake):
    root = tmp_path / "music"
    album = root / "Boards of Canada" / "Geogaddi (2002)"
    album.mkdir(parents=True)
    (album / "01 Ready Lets Go.flac").write_bytes(b"x" * 1000)
    (album / "cover.jpg").write_bytes(b"y" * 10)
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, music_dir=str(root)))
    lib_db.upsert_many([{"navidrome_id": "nd-1", "name": "Geogaddi", "artist": "Boards of Canada",
                         "mbz_release_group_id": RG, "folder": "Boards of Canada/Geogaddi (2002)",
                         "norm_key": album_key("Boards of Canada", "Geogaddi")}])
    fake.on("GET", f"{LIDARR}/album", [{"id": 5, "foreignAlbumId": RG, "monitored": True, "artistId": 9}])
    fake.on("PUT", f"{LIDARR}/album/monitor", {}, status=202)
    fake.on("POST", f"{LIDARR}/command", {"id": 1})
    fake.on("GET", f"{LIDARR}/album/5", {"id": 5, "foreignAlbumId": RG, "monitored": True, "artistId": 9})
    fake.on("GET", "http://navidrome.test/rest/startScan", {"subsonic-response": {"status": "ok", "scanStatus": {}}})
    return root


def _monitor_calls(fake):
    return [fake.body(r)["monitored"] for r in fake.called("PUT", f"{LIDARR}/album/monitor")]


def test_remove_moves_the_album_into_the_cache_and_restore_puts_it_back(client, fake, music):
    make_user()
    s = login(client)
    assert "library_removal" in client.get("/v1/me", headers=bearer(s)).json()["capabilities"]

    r = client.post("/v1/library/albums/nd-1/remove", headers=bearer(s))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["title"] == "Geogaddi" and out["removedBy"] == "isaac" and out["bytes"] == 1010
    assert not (music / "Boards of Canada" / "Geogaddi (2002)").exists()
    item = cache_db.get(out["id"])
    assert os.path.isfile(os.path.join(cache.cache_dir(), item["cache_path"], "01 Ready Lets Go.flac"))
    assert os.path.isfile(os.path.join(cache.cache_dir(), ".ndignore")), "Navidrome must skip the cache"
    assert lib_db.get("nd-1") is None
    assert _monitor_calls(fake) == [False], "unmonitored so Lidarr never downloads it again"

    listed = client.get("/v1/library/removed", headers=bearer(s)).json()
    assert [i["title"] for i in listed] == ["Geogaddi"] and listed[0]["restorableUntil"]

    r = client.post(f"/v1/library/removed/{out['id']}/restore", headers=bearer(s))
    assert r.status_code == 200, r.text
    assert (music / "Boards of Canada" / "Geogaddi (2002)" / "01 Ready Lets Go.flac").exists()
    assert cache_db.get(out["id"]) is None
    assert _monitor_calls(fake) == [False, True], "monitored again, as it was before"


def test_a_server_can_open_removal_to_every_account(client, fake, music, monkeypatch):
    make_user("admin")
    make_user("isaac", role="user")
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, library_removal_role="user"))
    s = login(client, "isaac")
    assert "library_removal" in client.get("/v1/me", headers=bearer(s)).json()["capabilities"]
    r = client.post("/v1/library/albums/nd-1/remove", headers=bearer(s))
    assert r.status_code == 200, r.text
    assert r.json()["removedBy"] == "isaac"


def test_removal_is_for_admins_by_default_and_others_get_no_button(client, fake, music):
    make_user("admin")
    make_user("bob", role="user")
    s = login(client, "bob")
    assert "library_removal" not in client.get("/v1/me", headers=bearer(s)).json()["capabilities"]
    assert client.post("/v1/library/albums/nd-1/remove", headers=bearer(s)).status_code == 403
    assert (music / "Boards of Canada" / "Geogaddi (2002)").exists()


def test_unsafe_and_shared_folders_are_refused(client, fake, music):
    make_user()
    s = login(client)
    lib_db.upsert_many([{"navidrome_id": "nd-2", "name": "Loose", "artist": "Boards of Canada",
                         "folder": "Boards of Canada", "norm_key": "x"},
                        {"navidrome_id": "nd-3", "name": "Geogaddi Bonus", "artist": "Boards of Canada",
                         "folder": "Boards of Canada/Geogaddi (2002)", "norm_key": "y"},
                        {"navidrome_id": "nd-4", "name": "Escape", "artist": "x",
                         "folder": "../outside/Album", "norm_key": "z"}])
    assert client.post("/v1/library/albums/nd-2/remove", headers=bearer(s)).json()["error"]["code"] == "unsafe_folder"
    assert client.post("/v1/library/albums/nd-1/remove", headers=bearer(s)).json()["error"]["code"] == "shared_folder"
    assert client.post("/v1/library/albums/nd-4/remove", headers=bearer(s)).json()["error"]["code"] == "unsafe_folder"
    assert (music / "Boards of Canada" / "Geogaddi (2002)").exists()
    assert not fake.called("PUT", f"{LIDARR}/album/monitor"), "nothing touched before the checks pass"


def test_restore_refuses_a_taken_folder(client, fake, music):
    make_user()
    s = login(client)
    out = client.post("/v1/library/albums/nd-1/remove", headers=bearer(s)).json()
    (music / "Boards of Canada" / "Geogaddi (2002)").mkdir()
    r = client.post(f"/v1/library/removed/{out['id']}/restore", headers=bearer(s))
    assert r.status_code == 409 and r.json()["error"]["code"] == "path_taken"
    assert cache_db.get(out["id"]) is not None, "still restorable once the folder is cleared"


def test_without_a_music_folder_removal_is_off(client, fake, monkeypatch):
    make_user()
    s = login(client)
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, music_dir=""))
    assert "library_removal" not in client.get("/v1/me", headers=bearer(s)).json()["capabilities"]
    lib_db.upsert_many([{"navidrome_id": "nd-1", "name": "G", "artist": "B", "folder": "B/G", "norm_key": "k"}])
    r = client.post("/v1/library/albums/nd-1/remove", headers=bearer(s))
    assert r.status_code == 503 and r.json()["error"]["code"] == "removal_unavailable"


def _age(item_id: int, days: float) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as c:
        c.execute("UPDATE media_cache SET created_at=?, last_used_at=? WHERE id=?", (ts, ts, item_id))


def _cached(name: str, nbytes: int, origin: str = "removed") -> dict:
    d = os.path.join(cache.cache_dir(), name)
    os.makedirs(d)
    with open(os.path.join(d, "a.flac"), "wb") as f:
        f.write(b"z" * nbytes)
    return cache_db.add({"kind": "album", "origin": origin, "title": name, "cache_path": name, "bytes": nbytes})


def test_eviction_is_least_recently_used_and_spares_the_undo_window(fake, music, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        config.settings, media_cache_max_gb=0, removed_min_days=7, removed_max_days=30))
    fresh = _cached("fresh", 10)              # removed today: inside the undo window
    old = _cached("old", 10)
    _age(old["id"], 10)                       # past the window: evictable for space
    stream = _cached("stream", 10, origin="stream")
    _age(stream["id"], 1)
    dropped = {i["title"] for i in cache.evict()}
    assert dropped == {"old", "stream"}
    assert cache_db.get(fresh["id"]) is not None
    assert not os.path.exists(os.path.join(cache.cache_dir(), "old"))


def test_removed_albums_go_after_the_maximum_age_even_with_room(fake, music, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        config.settings, media_cache_max_gb=100, removed_max_days=30))
    keep = _cached("keep", 10)
    _age(keep["id"], 20)
    gone = _cached("gone", 10)
    _age(gone["id"], 31)
    assert {i["title"] for i in cache.evict()} == {"gone"}


def test_set_role_makes_an_everyday_account_an_admin(fake):
    from continuo import cli
    from continuo.db import users
    make_user("admin")
    make_user("isaac", role="user")
    assert cli.main(["set-role", "isaac", "admin"]) == 0
    assert users.get("isaac")["role"] == "admin"
    assert cli.main(["set-role", "nobody", "admin"]) == 1
    assert cli.main(["set-role", "isaac", "user"]) == 0
    assert cli.main(["set-role", "admin", "user"]) == 1, "the last admin stays an admin"
