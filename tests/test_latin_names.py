"""Latin names for non-Latin artists (NAME_LOCALE), from MusicBrainz's own primary aliases."""
import dataclasses
import struct

import pytest
from conftest import LIDARR, bearer, login, make_user, run

from continuo import cli, config
from continuo.db import identity
from continuo.db import library as lib_db
from continuo.library import naming
from continuo.search import merge, resolve
from continuo.upstream import musicbrainz

FISH = "d0f25f16-7782-42e8-8047-c16a1cb2b450"
AOBA = "fc3ae4dd-dcc5-4f99-a865-6bc1a5c9b705"
HOOK = {"X-Webhook-Secret": "hook-secret"}
MB_FISH = {"name": "フィッシュマンズ", "sort-name": "Fishmans", "aliases": [
    {"name": "Fishmans", "locale": "en", "primary": True}, {"name": "Fishmans", "locale": "fr", "primary": True}]}
MB_AOBA = {"name": "青葉市子", "sort-name": "Aoba, Ichiko", "aliases": [
    {"name": "Aoba Ichiko", "locale": None, "primary": None},
    {"name": "Ichiko Aoba", "locale": "en", "primary": True},
    {"name": "青葉市子", "locale": "ja", "primary": True}]}


@pytest.fixture(autouse=True)
def _fresh():
    musicbrainz._artist_cache.clear()
    resolve._facts_cache.clear()
    yield


def _mb(fake):
    fake.on("GET", f"https://musicbrainz.org/ws/2/artist/{FISH}", MB_FISH)
    fake.on("GET", f"https://musicbrainz.org/ws/2/artist/{AOBA}", MB_AOBA)


def _norm(mb):
    return {"name": mb["name"], "sortName": mb["sort-name"],
            "aliases": [{"name": a["name"], "locale": a["locale"], "primary": bool(a["primary"])} for a in mb["aliases"]]}


def test_scripts():
    assert naming.is_latin("Sigur Rós") and naming.is_latin("AC/DC") and naming.is_latin("2814")
    assert not naming.is_latin("青葉市子") and not naming.is_latin("Мумий Тролль")


def test_the_primary_alias_in_the_language_is_the_name():
    assert naming.pick(_norm(MB_AOBA), "en") == {"name": "Ichiko Aoba", "sort": "Aoba, Ichiko", "original": "青葉市子"}
    assert naming.pick(_norm(MB_FISH), "en") == {"name": "Fishmans", "sort": "Fishmans", "original": "フィッシュマンズ"}
    assert naming.pick(_norm(MB_FISH), "original") is None
    assert naming.pick({"name": "Boards of Canada", "sortName": "Boards of Canada", "aliases": []}, "en") is None
    no_en = {"name": "青葉市子", "sortName": "青葉市子", "aliases": [{"name": "青葉市子", "locale": "ja", "primary": True}]}
    assert naming.pick(no_en, "en") is None, "never guessed or transliterated"


def _flac(path, artist):
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
    return {k: f[k] for k in ("artist", "albumartist", "artistsort", "albumartistsort") if k in f}


def test_an_import_by_a_non_latin_artist_gets_the_latin_name(client, fake, tmp_path, monkeypatch):
    music = tmp_path / "music"
    track = music / "フィッシュマンズ" / "空中キャンプ" / "01.flac"
    _flac(track, "フィッシュマンズ")
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, music_dir=str(music)))
    _mb(fake)
    fake.on("GET", f"https://api.lidarr.audio/api/v0.4/artist/{FISH}", {"artistname": "フィッシュマンズ", "artistaliases": ["Fishmans"]})
    r = client.post("/internal/webhooks/lidarr", headers=HOOK, json={
        "eventType": "Download", "isUpgrade": False, "artist": {"name": "フィッシュマンズ", "mbId": FISH},
        "album": {"id": 3, "mbId": "rg", "title": "空中キャンプ"},
        "trackFiles": [{"path": "/data/music/フィッシュマンズ/空中キャンプ/01.flac"}]})
    assert r.status_code == 200, r.text
    assert _tags(track) == {"artist": ["Fishmans"], "albumartist": ["Fishmans"],
                            "artistsort": ["Fishmans"], "albumartistsort": ["Fishmans"]}
    assert identity.artist_names()[0]["original_name"] == "フィッシュマンズ"


def test_original_names_keep_their_artists_when_asked(client, fake, tmp_path, monkeypatch):
    music = tmp_path / "music"
    track = music / "フィッシュマンズ" / "x" / "01.flac"
    _flac(track, "フィッシュマンズ")
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, music_dir=str(music), name_locale="original"))
    fake.on("GET", f"https://api.lidarr.audio/api/v0.4/artist/{FISH}", {"artistname": "フィッシュマンズ", "artistaliases": []})
    client.post("/internal/webhooks/lidarr", headers=HOOK, json={
        "eventType": "Download", "isUpgrade": False, "artist": {"name": "フィッシュマンズ", "mbId": FISH},
        "album": {"id": 3, "mbId": "rg", "title": "x"}, "trackFiles": [{"path": "/data/music/フィッシュマンズ/x/01.flac"}]})
    assert _tags(track)["artist"] == ["フィッシュマンズ"]


def test_latin_names_retags_only_what_lidarr_downloaded(fake, tmp_path, monkeypatch, capsys):
    music = tmp_path / "music"
    downloaded = music / "フィッシュマンズ" / "宇宙 日本 世田谷" / "01.flac"
    own = music / "フィッシュマンズ" / "Own Rip" / "01.flac"
    _flac(downloaded, "フィッシュマンズ")
    _flac(own, "フィッシュマンズ")
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, music_dir=str(music)))
    lib_db.upsert_many([
        {"navidrome_id": "nd-1", "name": "宇宙 日本 世田谷", "artist": "フィッシュマンズ", "folder": "フィッシュマンズ/宇宙 日本 世田谷", "norm_key": "a"},
        {"navidrome_id": "nd-2", "name": "Own Rip", "artist": "フィッシュマンズ", "folder": "フィッシュマンズ/Own Rip", "norm_key": "b"},
        {"navidrome_id": "nd-3", "name": "Geogaddi", "artist": "Boards of Canada", "folder": "Boards of Canada/Geogaddi", "norm_key": "c"},
    ])
    _mb(fake)
    fake.on("GET", f"{LIDARR}/artist/lookup", [{"foreignArtistId": FISH, "artistName": "フィッシュマンズ"}])
    fake.on("GET", f"{LIDARR}/artist", [{"id": 5, "foreignArtistId": FISH}])
    fake.on("GET", f"https://api.lidarr.audio/api/v0.4/artist/{FISH}", {"artistname": "フィッシュマンズ", "Albums": []})
    fake.on("GET", f"{LIDARR}/history/artist", [
        {"data": {"droppedPath": "/data/downloads/soulseek/Fishmans - 1997/01.flac",
                  "importedPath": "/data/music/フィッシュマンズ/宇宙 日本 世田谷/01.flac"}},
        {"data": {"droppedPath": "/home/isaac/rips/Own Rip/01.flac",
                  "importedPath": "/data/music/フィッシュマンズ/Own Rip/01.flac"}},
    ])
    fake.on("GET", "http://navidrome.test/rest/startScan", {"subsonic-response": {"status": "ok"}})

    assert cli.main(["latin-names"]) == 0
    out = capsys.readouterr().out
    assert "フィッシュマンズ  ->  Fishmans" in out and "retag   宇宙 日本 世田谷" in out and "leave   Own Rip" in out
    assert _tags(downloaded)["artist"] == ["フィッシュマンズ"], "a preview changes nothing"

    assert cli.main(["latin-names", "--apply"]) == 0
    assert _tags(downloaded)["albumartist"] == ["Fishmans"]
    assert _tags(own)["albumartist"] == ["フィッシュマンズ"], "the user's own copy is never touched"
    assert identity.library_name(FISH) == "Fishmans"


def test_searching_the_original_name_finds_the_library_artist(fake, monkeypatch):
    identity.set_library_name(FISH, "Fishmans", original_name="フィッシュマンズ", sort_name="Fishmans")
    asked = []

    async def search3(q, user, **kw):
        asked.append(q)
        return {"artist": [{"id": "ar-fish", "name": "Fishmans", "albumCount": 2}], "album": [], "song": []}
    monkeypatch.setattr(merge.navidrome, "search3", search3)
    library = {"artist": [], "album": [], "song": []}
    run(merge._add_renamed_library_artists(library, [{"musicBrainzArtistId": FISH, "name": "フィッシュマンズ"}], "isaac"))
    assert asked == ["Fishmans"]
    out = merge.merge_artists(library["artist"], [{"musicBrainzArtistId": FISH, "name": "フィッシュマンズ"}], 10)
    assert [(a["name"], a["ids"]["navidromeArtistId"]) for a in out] == [("Fishmans", "ar-fish")]


def test_artist_names_lists_both_names(client, fake):
    make_user()
    s = login(client)
    identity.set_library_name(AOBA, "Ichiko Aoba", original_name="青葉市子", sort_name="Aoba, Ichiko")
    identity.set_library_name("plain", "Peach Pit")
    r = client.get("/v1/library/artist-names", headers=bearer(s))
    assert r.json() == [{"name": "Ichiko Aoba", "originalName": "青葉市子", "sortName": "Aoba, Ichiko",
                         "musicBrainzArtistId": AOBA}]
