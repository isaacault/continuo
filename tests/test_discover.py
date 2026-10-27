"""Home's discovery: new from your artists, and for you."""
import asyncio

from continuo.search import discover
from continuo.upstream import lidarr, listenbrainz

FRESH = [
    {"release_group_mbid": "rg-outsiders", "release_name": "Outsiders", "artist_credit_name": "Juice WRLD",
     "release_date": "2026-10-22", "release_group_primary_type": "Album", "caa_release_mbid": "caa-o",
     "release_mbid": "rel-o", "artist_mbids": ["jw"]},
    {"release_group_mbid": "rg-sun", "release_name": "Staring at the Sun", "artist_credit_name": "FISHER & Laurel",
     "release_date": "2026-10-16", "release_group_primary_type": "Single", "caa_release_mbid": None,
     "release_mbid": "rel-s", "artist_mbids": ["f", "l"]},
    {"release_group_mbid": "rg-have", "release_name": "Already Here", "artist_credit_name": "Juice WRLD",
     "release_date": "2026-10-23", "release_group_primary_type": "Album", "caa_release_mbid": None,
     "release_mbid": "rel-h", "artist_mbids": ["jw"]},
    {"release_group_mbid": "rg-other", "release_name": "Someone Else", "artist_credit_name": "Nobody You Know",
     "release_date": "2026-10-25", "release_group_primary_type": "Album", "caa_release_mbid": None,
     "release_mbid": "rel-x", "artist_mbids": ["n"]},
]
YOURS = [{"artist": "Juice WRLD", "mbz_release_group_id": "rg-have"}, {"artist": "Laurel", "mbz_release_group_id": "rg-l"}]


def test_new_releases_are_your_artists_ones_you_don_t_have_newest_first():
    out = discover.new_from(YOURS, FRESH, limit=10)
    assert [a["title"] for a in out] == ["Outsiders", "Staring at the Sun"]
    assert out[0]["imageUrl"].endswith("/release/caa-o/front-500")


def test_an_artist_credit_names_each_of_its_artists():
    assert {"fisher", "laurel"} <= discover.credited("FISHER & Laurel")
    assert {"drake", "future"} <= discover.credited("Drake feat. Future")
    from continuo.requests.normalise import normalise
    assert normalise("Florence + the Machine") in discover.credited("Florence + the Machine"), "the band, whole"


def test_for_you_is_similar_artists_you_don_t_have_and_their_top_songs(monkeypatch):
    async def search_artists(name):
        return [{"name": name, "musicBrainzArtistId": f"mb-{name}"}]

    async def similar(mbid, limit=15):
        return [{"musicBrainzArtistId": "mb-Laurel", "name": "Laurel"},          # yours: left out
                {"musicBrainzArtistId": "mb-new", "name": "New One"}]

    async def top(mbid, limit=2):
        songs = [("Hit 0", "New One"), ("Hit 0", "New One"), ("Hit 1 (Nero Remix)", "New One"), ("Hit 1", "New One"),
                 ("Hit 2", "New One feat. X"), ("Their Song", "Laurel")]
        return [{"title": t, "artist": a, "durationMs": 1, "recordingMbid": f"rec-{i}", "releaseMbid": "rel",
                 "releaseName": "R"} for i, (t, a) in enumerate(songs)]
    monkeypatch.setattr(lidarr, "search_artists", search_artists)
    monkeypatch.setattr(listenbrainz, "similar_artists", similar)
    monkeypatch.setattr(listenbrainz, "top_recordings", top)
    out = asyncio.run(discover._for_you(YOURS, limit=10))
    assert [t["title"] for t in out] == ["Hit 0", "Hit 1"], "no repeats, no remixes, two per artist, none of yours"


def test_a_release_without_a_cover_shows_its_artist_s_picture(monkeypatch):
    async def search_artists(name):
        return [{"name": "Juice WRLD", "musicBrainzArtistId": "jw", "imageUrl": "https://images.lidarr.audio/jw.jpg"}]
    monkeypatch.setattr(lidarr, "search_artists", search_artists)
    discover._artist_art_cache.clear()
    albums = discover.new_from([{"artist": "Juice WRLD", "mbz_release_group_id": None}],
                               [dict(FRESH[0], caa_release_mbid=None)], limit=5)
    out = asyncio.run(discover._with_covers(albums))
    assert out[0]["imageUrl"] == "https://images.lidarr.audio/jw.jpg"
