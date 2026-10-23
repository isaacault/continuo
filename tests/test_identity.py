"""One artist under two names (Ichiko Aoba in the library, 青葉市子 in MusicBrainz): resolving it,
folding it in search, matching its untagged albums, and filing new imports under the library's name."""
import dataclasses
import struct

import httpx
import pytest
from conftest import LIDARR, bearer, login, make_user

from continuo import config
from continuo.db import identity
from continuo.db import library as lib_db
from continuo.requests.normalise import album_key
from continuo.search import merge, resolve

AOBA = "fc3ae4dd-dcc5-4f99-a865-6bc1a5c9b705"
ICHIKO = "f5477dbf-c1fa-4fba-9368-fa57cdc82372"
RG_ADAN = "6c1d0a3c-0000-4000-8000-0000000000ad"
RG_ZERO = "de63344e-b5eb-4e79-abe1-7c82f40801d9"
RG_LUMI = "6c1d0a3c-0000-4000-8000-00000000001c"
HOOK = {"X-Webhook-Secret": "hook-secret"}
META = f"https://api.lidarr.audio/api/v0.4/artist/{AOBA}"


@pytest.fixture(autouse=True)
def _fresh():
    resolve._facts_cache.clear()
    yield


def _library():
    lib_db.upsert_many([
        {"navidrome_id": "nd-adan", "name": "Windswept Adan", "artist": "Ichiko Aoba",
         "folder": "Ichiko Aoba/Windswept Adan", "norm_key": album_key("Ichiko Aoba", "Windswept Adan")},
        {"navidrome_id": "nd-lumi", "name": "Luminescent Creatures", "artist": "Ichiko Aoba",
         "folder": "Ichiko Aoba/Luminescent Creatures", "norm_key": album_key("Ichiko Aoba", "Luminescent Creatures")},
    ])


def _catalogue(fake):
    fake.on("GET", f"{LIDARR}/artist/lookup", [
        {"foreignArtistId": AOBA, "artistName": "青葉市子"},
        {"foreignArtistId": ICHIKO, "artistName": "ICHIKO"},
    ])
    fake.on("GET", f"{LIDARR}/artist", [])
    fake.on("GET", META, {"artistname": "青葉市子", "artistaliases": ["Aoba Ichiko", "Ichiko Aoba"], "Albums": [
        {"Id": RG_ADAN, "Title": "アダンの風", "Type": "Album", "ReleaseStatuses": ["Official"], "ReleaseDate": "2020-12-02"},
        {"Id": RG_ZERO, "Title": "0", "Type": "Album", "ReleaseStatuses": ["Official"], "ReleaseDate": "2013-10-23"},
        {"Id": RG_LUMI, "Title": "Luminescent Creatures", "Type": "Album", "ReleaseStatuses": ["Official"], "ReleaseDate": "2025-02-28"},
    ]})
    fake.on("GET", f"https://api.lidarr.audio/api/v0.4/artist/{ICHIKO}", {"artistname": "ICHIKO", "Albums": []})


def test_an_alias_names_the_library_artist(client, fake):
    make_user()
    s = login(client)
    _library()
    _catalogue(fake)
    r = client.get("/v1/catalog/artists/resolve", params={"name": "Ichiko Aoba"}, headers=bearer(s))
    assert r.status_code == 200, r.text
    assert r.json()["musicBrainzArtistId"] == AOBA and r.json()["sharedAlbums"] == 1
    assert identity.library_name(AOBA) == "Ichiko Aoba", "remembered for search and imports"


def test_search_folds_the_catalogue_name_into_the_library_artist(fake):
    identity.set_library_name(AOBA, "Ichiko Aoba")
    out = merge.merge_artists([{"id": "ar-1", "name": "Ichiko Aoba"}],
                              [{"musicBrainzArtistId": AOBA, "name": "青葉市子"}], 10)
    assert [a["name"] for a in out] == ["Ichiko Aoba"]
    assert out[0]["ids"]["musicBrainzArtistId"] == AOBA


def test_untagged_albums_are_matched_by_release_title(client, fake):
    make_user()
    s = login(client)
    _library()
    _catalogue(fake)
    identity.set_library_name(AOBA, "Ichiko Aoba")
    asked = []

    def releases(req: httpx.Request):
        asked.append(req.url.params["query"])
        return httpx.Response(200, json={"releases": [{"score": 100, "release-group": {"id": RG_ADAN}}]})
    fake.on("GET", "https://musicbrainz.org/ws/2/release", handler=releases)
    r = client.get(f"/v1/catalog/artists/{AOBA}/albums", headers=bearer(s))
    assert r.status_code == 200, r.text
    by_rg = {a["ids"]["musicBrainzReleaseGroupId"]: a["availability"] for a in r.json()}
    assert by_rg[RG_ADAN] == {"state": "in_library", "navidromeAlbumId": "nd-adan", "trackCount": None}
    assert by_rg[RG_LUMI]["state"] == "in_library", "same title, no lookup needed"
    assert by_rg[RG_ZERO]["state"] == "requestable"
    assert asked == [f'release:"Windswept Adan" AND arid:{AOBA}'], "one lookup, only for the untitled match"
    client.get(f"/v1/catalog/artists/{AOBA}/albums", headers=bearer(s))
    assert len(asked) == 1, "remembered"


def _flac(path, artist):
    """A minimal valid FLAC (STREAMINFO only) carrying artist tags."""
    from mutagen.flac import FLAC
    info = bytearray(34)
    struct.pack_into(">HH", info, 0, 4096, 4096)
    info[10:18] = ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 34]) + bytes(info))
    f = FLAC(str(path))
    f.add_tags()
    f["artist"] = [artist]
    f["albumartist"] = [artist]
    f.save()


def _tags(path):
    from mutagen.flac import FLAC
    f = FLAC(str(path))
    return f["artist"], f["albumartist"]


def test_an_import_is_filed_under_the_library_name(client, fake, tmp_path, monkeypatch):
    music = tmp_path / "music"
    track = music / "青葉市子" / "0" / "01 いきのこり●ぼくら.flac"
    _flac(track, "青葉市子")
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, music_dir=str(music)))
    _library()
    _catalogue(fake)
    r = client.post("/internal/webhooks/lidarr", headers=HOOK, json={
        "eventType": "Download", "isUpgrade": False,
        "artist": {"id": 7, "name": "青葉市子", "mbId": AOBA},
        "album": {"id": 9, "mbId": RG_ZERO, "title": "0"},
        "trackFiles": [{"path": "/data/music/青葉市子/0/01 いきのこり●ぼくら.flac"}]})
    assert r.status_code == 200, r.text
    assert _tags(track) == (["Ichiko Aoba"], ["Ichiko Aoba"])
    assert identity.library_name(AOBA) == "Ichiko Aoba", "found through the alias, then remembered"


def test_an_artist_the_library_names_the_same_way_is_left_alone(client, fake, tmp_path, monkeypatch):
    music = tmp_path / "music"
    track = music / "Peach Pit" / "Magpie" / "01.flac"
    _flac(track, "Peach Pit")
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, music_dir=str(music)))
    fake.on("GET", "https://api.lidarr.audio/api/v0.4/artist/pp", {"artistname": "Peach Pit", "artistaliases": []})
    client.post("/internal/webhooks/lidarr", headers=HOOK, json={
        "eventType": "Download", "isUpgrade": False, "artist": {"name": "Peach Pit", "mbId": "pp"},
        "album": {"id": 1, "mbId": "rg", "title": "Magpie"}, "trackFiles": [{"path": "/data/music/Peach Pit/Magpie/01.flac"}]})
    assert _tags(track) == (["Peach Pit"], ["Peach Pit"])


def test_merge_artist_files_existing_imports_under_the_library_name(fake, tmp_path, monkeypatch):
    from continuo import cli
    music = tmp_path / "music"
    track = music / "青葉市子" / "0" / "01.flac"
    _flac(track, "青葉市子")
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, music_dir=str(music)))
    lib_db.upsert_many([{"navidrome_id": "nd-0", "name": "0", "artist": "青葉市子", "folder": "青葉市子/0", "norm_key": "k"}])
    fake.on("GET", "http://navidrome.test/rest/startScan", {"subsonic-response": {"status": "ok"}})
    assert cli.main(["merge-artist", "青葉市子", "--into", "Ichiko Aoba", "--mbid", AOBA]) == 0
    assert _tags(track) == (["Ichiko Aoba"], ["Ichiko Aoba"])
    assert identity.library_name(AOBA) == "Ichiko Aoba"
