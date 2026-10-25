"""Per-user library folders (#27): hardlinks into the store, kept exact as the store changes."""
import asyncio
import dataclasses
import os

import pytest
from conftest import make_user

from continuo import config
from continuo.db import members as members_db
from continuo.library import links


@pytest.fixture
def media(tmp_path, monkeypatch, fake):
    store = tmp_path / "media" / "music"
    libs = tmp_path / "media" / "libraries"
    for album, files in {"Peach Pit/Being So Normal": ["01 Drop the Guillotine.flac", "02 Being So Normal.flac", "cover.jpg"],
                         "Ichiko Aoba/0": ["01 いきのこり●ぼくら.flac"],
                         "Fishmans/空中キャンプ": ["01 ずっと前.flac"]}.items():
        (store / album).mkdir(parents=True)
        for f in files:
            (store / album / f).write_bytes(f.encode())
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        config.settings, music_dir=str(store), libraries_dir=str(libs)))
    return store, libs


def _ino(p):
    return os.stat(p).st_ino


def test_a_member_album_is_hardlinked_into_the_account_s_folder(media):
    store, libs = media
    isaac = make_user("isaac", role="user")
    members_db.add(isaac["id"], "Peach Pit/Being So Normal")
    asyncio.run(links.tick())
    mine = libs / "isaac" / "Peach Pit" / "Being So Normal" / "01 Drop the Guillotine.flac"
    assert _ino(mine) == _ino(store / "Peach Pit" / "Being So Normal" / "01 Drop the Guillotine.flac")
    assert (libs / "isaac" / "Peach Pit" / "Being So Normal" / "cover.jpg").exists()
    assert not (libs / "isaac" / "Fishmans").exists(), "only what the account has"


def test_a_file_tagscrub_replaced_is_relinked(media):
    store, libs = media
    isaac = make_user("isaac", role="user")
    members_db.add(isaac["id"], "Ichiko Aoba/0")
    asyncio.run(links.tick())
    src = store / "Ichiko Aoba" / "0" / "01 いきのこり●ぼくら.flac"
    tmp = src.parent / ".01.tagscrub"
    tmp.write_bytes(b"scrubbed")
    os.replace(tmp, src)                     # copy + rename: a new inode, as tagscrub does
    stats = links.sync_user(isaac, *links.roots())
    mine = libs / "isaac" / "Ichiko Aoba" / "0" / "01 いきのこり●ぼくら.flac"
    assert stats.relinked == 1 and _ino(mine) == _ino(src) and mine.read_bytes() == b"scrubbed"


def test_strays_and_removed_albums_leave_the_folder(media):
    store, libs = media
    isaac = make_user("isaac", role="user")
    members_db.add(isaac["id"], "Peach Pit/Being So Normal")
    members_db.add(isaac["id"], "Fishmans/空中キャンプ")
    asyncio.run(links.tick())
    (libs / "isaac" / "stray.txt").write_text("x")
    members_db.remove(isaac["id"], "Fishmans/空中キャンプ")
    asyncio.run(links.tick())
    assert not (libs / "isaac" / "Fishmans").exists()
    assert not (libs / "isaac" / "stray.txt").exists()
    assert (libs / "isaac" / "Peach Pit" / "Being So Normal").is_dir()
    assert (store / "Fishmans" / "空中キャンプ" / "01 ずっと前.flac").exists(), "the store is never touched"


def test_an_album_gone_from_the_store_goes_from_the_library(media):
    import shutil
    store, libs = media
    isaac = make_user("isaac", role="user")
    members_db.add(isaac["id"], "Fishmans/空中キャンプ")
    asyncio.run(links.tick())
    shutil.rmtree(store / "Fishmans")
    asyncio.run(links.tick())
    assert not (libs / "isaac" / "Fishmans" / "空中キャンプ").exists()


def test_two_accounts_share_one_copy(media):
    store, libs = media
    a, b = make_user("isaac", role="user"), make_user("guest", role="user")
    for u in (a, b):
        members_db.add(u["id"], "Peach Pit/Being So Normal")
    asyncio.run(links.tick())
    f = "Peach Pit/Being So Normal/02 Being So Normal.flac"
    assert _ino(libs / "isaac" / f) == _ino(libs / "guest" / f) == _ino(store / f)
    assert members_db.holders("Peach Pit/Being So Normal") == {a["id"], b["id"]}


def test_unsafe_folders_are_refused(media):
    store, libs = media
    root = links.library_root(str(libs), "isaac")
    for bad in ("../outside", "/etc", "", ".continuo-cache/removed-1"):
        with pytest.raises(ValueError):
            links.sync_album(str(store), root, bad)


def test_off_without_libraries_dir(media, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, libraries_dir=""))
    with pytest.raises(links.Unavailable):
        links.roots()
    asyncio.run(links.tick())   # a no-op, not an error


def test_an_album_from_outside_a_request_goes_to_the_unclaimed_account(media, monkeypatch):
    """Drake's Comeback Season came in through Lidarr directly, after the migration: nobody's
    library had it, so nobody could see it."""
    import time

    from continuo.db import account_libraries as acc_db
    from continuo.db import library as lib_db
    store, libs = media
    isaac = make_user("isaac", role="user")
    acc_db.set(isaac["id"], 3, None)
    members_db.add(isaac["id"], "Ichiko Aoba/0")
    lib_db.upsert_many([
        {"navidrome_id": "a", "name": "0", "folder": "Ichiko Aoba/0", "norm_key": "a", "library_id": 1},
        {"navidrome_id": "b", "name": "Being So Normal", "folder": "Peach Pit/Being So Normal", "norm_key": "b",
         "library_id": 1},
        {"navidrome_id": "c", "name": "空中キャンプ", "folder": "Fishmans/空中キャンプ", "norm_key": "c", "library_id": 1}])
    old = time.time() - 3600
    os.utime(store / "Peach Pit" / "Being So Normal", (old, old))      # Fishmans' just arrived
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, library_unclaimed_to=("isaac",)))
    asyncio.run(links.tick())
    assert members_db.folders(isaac["id"]) == {"Ichiko Aoba/0", "Peach Pit/Being So Normal"}
    assert (libs / "isaac" / "Peach Pit" / "Being So Normal" / "cover.jpg").exists()
    assert members_db.holders("Fishmans/空中キャンプ") == set(), "a request may still claim it"


def test_nobody_adopts_without_the_setting(media):
    from continuo.db import library as lib_db
    lib_db.upsert_many([{"navidrome_id": "b", "name": "B", "folder": "Peach Pit/Being So Normal", "norm_key": "b"}])
    assert links.adopt_unclaimed(str(media[0])) == 0
