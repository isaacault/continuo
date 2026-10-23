"""Single songs never make a partial album for Lidarr (#24): they live in the singles folder,
show as "1 of N" with the album still requestable, and go when the whole album arrives."""
import dataclasses

import pytest
from conftest import make_user

from continuo import config
from continuo.db import library as lib_db
from continuo.db import members as members_db
from continuo.db import requests as req_db
from continuo.library import singles
from continuo.search import merge

RG = "22222222-0000-4000-8000-000000000002"
REC = "11111111-0000-4000-8000-000000000001"
SINGLES = "_singles/Peach Pit/Being So Normal"


@pytest.fixture
def store(tmp_path, monkeypatch, fake):
    root = tmp_path / "media" / "music"
    (root / SINGLES).mkdir(parents=True)
    (root / SINGLES / "02 Drop the Guillotine.flac").write_bytes(b"single")
    (root / "Peach Pit" / "Being So Normal").mkdir(parents=True)
    (root / "Peach Pit" / "Being So Normal" / "02 Drop the Guillotine.flac").write_bytes(b"album")
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, music_dir=str(root)))
    lib_db.upsert_many([{"navidrome_id": "nd-single", "name": "Being So Normal", "artist": "Peach Pit",
                         "mbz_release_group_id": RG, "folder": SINGLES, "norm_key": "k", "song_count": 1}])
    isaac = make_user("isaac", role="user")
    row = req_db.create(mb_release_group_id=REC, title="Drop the Guillotine", artist="Peach Pit", year=2017,
                        artwork_url=None, user_id=isaac["id"], kind="track")
    req_db.update(row["id"], status="available", album_release_group_id=RG, track={"trackCount": 10},
                  file_path=f"{SINGLES}/02 Drop the Guillotine.flac")
    return root, isaac, row["id"]


def test_an_album_of_singles_is_partial_and_still_requestable(store):
    index = merge._LibraryIndex(lib_db.all_albums())
    out = merge.merge_albums([], [{"musicBrainzReleaseGroupId": RG, "title": "Being So Normal",
                                   "artist": "Peach Pit", "releaseIds": []}], index, {}, 10)
    assert out[0]["availability"] == {"state": "partial", "have": 1, "expected": 10,
                                      "navidromeAlbumId": "nd-single", "requestable": True}


def test_its_other_songs_can_still_be_requested(store):
    index = merge._LibraryIndex(lib_db.all_albums())
    lib = lib_db.get("nd-single")
    assert index.song_album(lib, REC) is lib, "the song that's there opens it"
    assert index.song_album(lib, "another-recording") is None


def test_singles_are_never_taken_for_the_album(store):
    assert singles.whole(lib_db.by_release_group(RG)) == [], "requesting the album isn't 'already in library'"


def test_the_whole_album_arriving_absorbs_its_singles(store):
    root, isaac, rid = store
    members_db.add(isaac["id"], SINGLES)
    assert singles.absorb(RG, "Peach Pit/Being So Normal") == 1
    assert not (root / "_singles" / "Peach Pit").exists()
    assert (root / "Peach Pit" / "Being So Normal" / "02 Drop the Guillotine.flac").read_bytes() == b"album"
    assert members_db.folders(isaac["id"]) == {"Peach Pit/Being So Normal"}, "had the song: has the album"
    assert req_db.get(rid)["file_path"] is None
    assert singles.absorb(RG, "Peach Pit/Being So Normal") == 0, "once"


async def _unexpected(*a, **k):
    raise AssertionError("Lidarr must not be asked about single songs")


def test_removing_singles_leaves_lidarr_alone(store, monkeypatch):
    import asyncio

    from continuo.library import removal
    from continuo.upstream import lidarr, navidrome
    monkeypatch.setattr(lidarr, "find_album_by_foreign_id", _unexpected)
    monkeypatch.setattr(lidarr, "set_album_monitored", _unexpected)

    async def scan(full=False):
        return {}
    monkeypatch.setattr(navidrome, "start_scan", scan)
    out = asyncio.run(removal.remove("nd-single", "isaac"))
    assert out["title"] == "Being So Normal"
