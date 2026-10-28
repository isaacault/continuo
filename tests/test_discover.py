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
    async def go():
        picks, yours = await discover._similar(YOURS)
        return await discover._for_you(picks, yours, limit=10)
    out = asyncio.run(go())
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


LBAPI = "https://api.listenbrainz.org/1"


def _pl(pid, kind, date_, title):
    return {"playlist": {"identifier": f"https://listenbrainz.org/playlist/{pid}", "title": title, "date": date_,
                         "annotation": "<p>Made <b>for you</b></p>",
                         "extension": {"https://musicbrainz.org/doc/jspf#playlist": {"additional_metadata": {
                             "algorithm_metadata": {"source_patch": kind}}}}}}


def test_listenbrainz_playlists_the_newest_of_each_kind(fake):
    fake.on("GET", f"{LBAPI}/user/isaac_lb/playlists/createdfor", {"playlists": [
        _pl("a", "daily-jams", "2026-10-27T23:00:00", "Daily Jams 27"), _pl("b", "daily-jams", "2026-10-28T23:00:00", "Daily Jams 28"),
        _pl("c", "weekly-exploration", "2026-10-26T00:00:00", "Weekly Exploration")]})
    out = asyncio.run(listenbrainz.created_for("isaac_lb"))
    assert [p["title"] for p in out] == ["Daily Jams 28", "Weekly Exploration"]
    assert out[0]["description"] == "Made for you"


def test_a_listenbrainz_playlist_becomes_songs(fake, monkeypatch):
    fake.on("GET", f"{LBAPI}/playlist/11111111-2222-3333-4444-555555555555", {"playlist": {"title": "Weekly Jams", "track": [
        {"title": "Bless This Morning Year", "creator": "Helios", "album": "Eingya", "duration": "361880",
         "identifier": ["https://musicbrainz.org/recording/rec-1"],
         "extension": {"https://musicbrainz.org/doc/jspf#track": {"additional_metadata": {"caa_release_mbid": "caa-1"}}}}]}})

    async def releases(ids):
        return {"rec-1": {"releaseGroupMbid": "rg-1", "name": "Eingya", "year": 2006, "caaReleaseMbid": None}}
    monkeypatch.setattr(listenbrainz, "recording_releases", releases)
    page = asyncio.run(discover.playlist_page(None, "11111111-2222-3333-4444-555555555555"))
    song = page["tracks"][0]
    assert page["title"] == "Weekly Jams" and song["key"] == "mb:rec:rec-1" and song["album"]["musicBrainzReleaseGroupId"] == "rg-1"
    assert song["durationMs"] == 361880


def test_playlists_need_a_linked_listenbrainz_account(fake):
    assert asyncio.run(discover._playlists(None)) == []



def test_an_expired_page_is_served_while_a_fresh_one_is_built(monkeypatch):
    built = []

    async def build(user_id, limit):
        built.append(1)
        return {"n": len(built)}
    monkeypatch.setattr(discover, "_build", build)
    discover._page_cache.clear()
    discover._last_page.clear()

    async def go():
        first = await discover.page(1)
        discover._page_cache.clear()                 # three hours later
        stale = await discover.page(1)
        await asyncio.gather(*discover._warming)
        return first, stale, await discover.page(1)
    first, stale, fresh = asyncio.run(go())
    assert first == {"n": 1} and stale == {"n": 1} and fresh == {"n": 2}



def test_seeds_come_from_what_you_play_on_last_fm(monkeypatch, fake):
    import dataclasses

    from continuo import config
    from continuo.db import scrobblers, users
    from continuo.upstream import scrobble
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, lastfm_api_key="K", lastfm_secret="S"))
    u = users.create("isaac", "x", "admin")
    scrobblers.put(u["id"], "lastfm", "SK", "isaac_fm")
    fake.on("GET", "https://ws.audioscrobbler.com/2.0/", {"topartists": {"artist": [
        {"name": "Jorja Smith", "mbid": "js-mbid", "playcount": "300"}, {"name": "Little Simz", "mbid": "", "playcount": "200"}]}})
    seeds = asyncio.run(discover.listening_seeds(u["id"]))
    assert seeds == [("Jorja Smith", "js-mbid"), ("Little Simz", None)]
    asked = []

    async def similar(mbid, limit=15):
        asked.append(mbid)
        return [{"musicBrainzArtistId": "new-1", "name": "Cleo Sol"}]

    async def search_artists(name):
        return [{"name": name, "musicBrainzArtistId": "ls-mbid"}]
    monkeypatch.setattr(listenbrainz, "similar_artists", similar)
    monkeypatch.setattr(lidarr, "search_artists", search_artists)
    monkeypatch.setattr(scrobble, "lastfm_top_artists", scrobble.lastfm_top_artists)
    picks, _yours = asyncio.run(discover._similar([], u["id"]))
    assert picks == [("new-1", "Cleo Sol")] and set(asked) <= {"js-mbid", "ls-mbid"} and asked


def test_pages_are_built_ahead_and_again_before_they_expire(monkeypatch, fake):
    from continuo.db import users
    built = []

    async def build(user_id, limit):
        built.append(user_id)
        return {"n": len(built)}
    monkeypatch.setattr(discover, "_build", build)
    discover._built_at.clear()
    a = users.create("isaac", "x", "admin")
    b = users.create("guest", "x", "user")
    asyncio.run(discover.warm())
    assert sorted(built) == sorted([a["id"], b["id"]]), "every account's page, before anyone asks"
    asyncio.run(discover.warm())
    assert len(built) == 2, "not again while fresh"
    discover._built_at[(a["id"], 12)] -= discover._WARM_AFTER_S + 1
    asyncio.run(discover.warm())
    assert built[-1] == a["id"] and len(built) == 3, "rebuilt before the cache runs out"
    assert asyncio.run(discover.page(a["id"])) == {"n": 3}, "served from what was built"



def test_the_same_release_twice_shows_once():
    twice = [dict(FRESH[1]), dict(FRESH[1], release_group_mbid="rg-sun-2", release_name="Staring At The Sun")]
    assert [a["title"] for a in discover.new_from(YOURS, twice, limit=10)] == ["Staring at the Sun"]


def test_a_new_release_without_a_cover_takes_deezer_s(monkeypatch):
    from continuo.upstream import deezer

    async def albums(q, limit=10):
        return [{"title": "Outsiders", "artist": "Juice WRLD", "recordType": "album", "cover": "https://cdn-images.dzcdn.net/o.jpg"}]
    monkeypatch.setattr(deezer, "search_albums", albums)
    out = asyncio.run(discover._with_covers([{"title": "Outsiders", "artist": "Juice WRLD", "imageUrl": None}]))
    assert out[0]["imageUrl"] == "https://cdn-images.dzcdn.net/o.jpg"
