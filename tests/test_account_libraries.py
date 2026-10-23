"""Each account's own Navidrome library (#27): created, filled from the store, then access."""
import asyncio
import dataclasses
import json

import httpx
import pytest
from conftest import make_user

from continuo import cli, config
from continuo.db import account_libraries as acc_db
from continuo.db import library as lib_db
from continuo.db import members as members_db
from continuo.library import accounts

ND = "http://navidrome.test"


@pytest.fixture
def media(tmp_path, monkeypatch, fake):
    store, libs = tmp_path / "media" / "music", tmp_path / "media" / "libraries"
    for album in ("Peach Pit/Being So Normal", "Ichiko Aoba/0"):
        (store / album).mkdir(parents=True)
        (store / album / "01.flac").write_bytes(b"x")
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        config.settings, music_dir=str(store), libraries_dir=str(libs)))
    lib_db.upsert_many([
        {"navidrome_id": "s-1", "name": "Being So Normal", "artist": "Peach Pit", "folder": "Peach Pit/Being So Normal", "norm_key": "a", "library_id": 1},
        {"navidrome_id": "s-2", "name": "0", "artist": "Ichiko Aoba", "folder": "Ichiko Aoba/0", "norm_key": "b", "library_id": 1},
        # an account library's copy of the same album must not count as another store album
        {"navidrome_id": "u-1", "name": "Being So Normal", "artist": "Peach Pit", "folder": "Peach Pit/Being So Normal", "norm_key": "a", "library_id": 2},
    ])
    created, granted = [], []

    def library(req: httpx.Request):
        if req.method == "POST":
            body = json.loads(req.content)
            created.append(body)
            return httpx.Response(200, json={"id": 2, **body})
        return httpx.Response(200, json=[{"id": 1, "name": "Music Library", "path": "/music"}]
                              + [{"id": 2, **b} for b in created])
    fake.on("GET", f"{ND}/api/library", handler=library)
    fake.on("POST", f"{ND}/api/library", handler=library)
    fake.on("GET", f"{ND}/api/user", [{"id": "nd-isaac", "userName": "isaac", "isAdmin": False}])
    fake.on("PUT", f"{ND}/api/user/nd-isaac/library", handler=lambda req: (granted.append(json.loads(req.content)),
                                                                            httpx.Response(200, json=[]))[1])
    return store, libs, created, granted


def test_matching_sees_only_the_store(media):
    assert {a["navidrome_id"] for a in lib_db.all_albums()} == {"s-1", "s-2"}
    assert [a["navidrome_id"] for a in lib_db.by_folder("Peach Pit/Being So Normal")] == ["s-1"]
    assert lib_db.folders_in_library(2) == {"Peach Pit/Being So Normal": "u-1"}


def test_init_all_gives_the_account_everything_and_its_own_navidrome_library(media):
    store, libs, created, granted = media
    isaac = make_user("isaac", role="user")
    out = asyncio.run(accounts.init(isaac, everything=True))
    assert created == [{"name": "isaac", "path": "/libraries/isaac"}]
    assert out["library"] == 2 and out["albums"] == 2 and out["linked"] == 2
    assert (libs / "isaac" / "Ichiko Aoba" / "0" / "01.flac").exists()
    assert members_db.folders(isaac["id"]) == {"Peach Pit/Being So Normal", "Ichiko Aoba/0"}
    assert acc_db.get(isaac["id"])["navidrome_user_id"] == "nd-isaac"
    assert granted == [], "access only changes on grant"
    asyncio.run(accounts.init(isaac, everything=True))
    assert len(created) == 1, "the library is created once"


def test_grant_gives_access_to_the_own_library_alone(media):
    *_, granted = media
    isaac = make_user("isaac", role="user")
    asyncio.run(accounts.grant_only_own(isaac))
    assert granted == [{"libraryIds": [2]}]


def test_a_new_account_starts_empty(media):
    store, libs, created, _ = media
    guest = make_user("guest", role="user")
    out = asyncio.run(accounts.init(guest, everything=False))
    assert out["albums"] == 0 and (libs / "guest").is_dir()


def test_cli_init_and_grant(media, capsys):
    make_user("isaac", role="user")
    assert cli.main(["libraries", "init", "isaac", "--all"]) == 0
    assert "2 album(s)" in capsys.readouterr().out
    assert cli.main(["libraries", "grant", "isaac"]) == 0
    assert cli.main(["libraries", "status"]) == 0
    assert "isaac" in capsys.readouterr().out
