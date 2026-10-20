from continuo.requests import matching


def lib(nid, name, artist="Boards of Canada", **kw):
    return {"navidrome_id": nid, "name": name, "artist": artist, "year": kw.get("year"),
            "song_count": kw.get("songs"), "mbz_album_id": kw.get("release"),
            "mbz_release_group_id": kw.get("rg"), "folder": kw.get("folder")}


ROW = {"mb_release_group_id": "rg-geo", "title": "Geogaddi", "artist": "Boards of Canada",
       "year": 2002, "lidarr_release_id": None, "import_paths": []}
LIDARR_ALBUM = {"releases": [{"foreignReleaseId": "rel-1", "monitored": True},
                             {"foreignReleaseId": "rel-2"}],
                "statistics": {"trackCount": 23}}


def test_paths_win_without_any_tags():
    library = [lib("a", "Geogaddi", folder="Boards of Canada/Geogaddi (2002)"),
               lib("b", "Music Has the Right to Children", folder="Boards of Canada/MHTRTC")]
    m = matching.match(ROW, library, paths=[
        "/data/music/Boards of Canada/Geogaddi (2002)/01 - Ready Lets Go.flac",
        "/data/music/Boards of Canada/Geogaddi (2002)/02 - Music Is Math.flac"])
    assert (m.strategy, m.navidrome_album_id) == ("paths", "a")


def test_multi_disc_folders():
    library = [lib("a", "Geogaddi", folder="BoC/Geogaddi")]
    m = matching.match(ROW, library, paths=["/data/music/BoC/Geogaddi/CD1/01.flac",
                                            "/data/music/BoC/Geogaddi/CD2/01.flac"])
    assert m.navidrome_album_id == "a"


def test_loose_artist_folder_does_not_swallow_everything():
    library = [lib("loose", "Some Single", folder="Boards of Canada")]
    m = matching.match(dict(ROW, title="Other"), library,
                       paths=["/data/music/Boards of Canada/Geogaddi/01.flac"])
    assert not m.matched


def test_release_group_then_release():
    library = [lib("a", "Geogaddi (Deluxe)", rg="rg-geo")]
    assert matching.match(ROW, library).strategy == "release_group"
    library = [lib("a", "Geogaddi?", release="rel-2")]
    m = matching.match(ROW, library, lidarr_album=LIDARR_ALBUM)
    assert (m.strategy, m.navidrome_album_id) == ("release", "a")


def test_two_editions_same_release_group_prefers_monitored_release():
    library = [lib("std", "Geogaddi", rg="rg-geo", release="rel-1"),
               lib("dlx", "Geogaddi (Deluxe)", rg="rg-geo", release="rel-9")]
    m = matching.match(ROW, library, lidarr_album=LIDARR_ALBUM)
    assert m.navidrome_album_id == "std"


def test_fuzzy_accepts_unique_strong_match():
    library = [lib("a", "Geogaddi", year=2002, songs=23),
               lib("b", "Campfire Headphase", year=2005, songs=15)]
    m = matching.match(ROW, library, lidarr_album=LIDARR_ALBUM)
    assert (m.strategy, m.navidrome_album_id) == ("fuzzy", "a")


def test_fuzzy_ambiguity_is_never_silently_linked():
    library = [lib("a", "Geogaddi", year=2002, songs=23, folder="x/1"),
               lib("b", "Geogaddi", year=2002, songs=23, folder="x/2")]
    m = matching.match(ROW, library, lidarr_album=LIDARR_ALBUM)
    assert not m.matched and m.ambiguous
    assert {c["navidromeAlbumId"] for c in m.candidates} == {"a", "b"}


def test_weak_title_only_match_is_ambiguous_not_linked():
    library = [lib("a", "Geogaddi", year=1990, songs=5)]
    m = matching.match(ROW, library, lidarr_album=LIDARR_ALBUM)
    assert not m.matched and m.ambiguous


def test_nothing_in_library():
    assert not matching.match(ROW, [lib("a", "Other")]).matched
