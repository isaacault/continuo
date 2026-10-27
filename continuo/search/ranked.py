"""The catalogue part of search, ranked by what people listen to.

Deezer's search finds and ranks (popular first, songs and albums by title, typos forgiven, about
0.15 s); its hits are then given MusicBrainz IDs, which everything else in continuo deals in:
- songs: ListenBrainz's lookup by artist and title, all at once, then ListenBrainz's recording
  metadata for each one's release group and cover, all at once (0.2-0.4 s each);
- artists and albums: the top songs' IDs where they're the same, else Lidarr's search (no rate
  limit) for the top few, matched by name.
A hit that can't be given IDs is left out: without them it can't be played now or requested.

`best` is the one hit the page leads with, chosen by what the top songs say was meant (see
candidates): the artist whose songs fill the top ("sabr"), the album they come from ("ok
computer"), else the top song ("espresso", "kendrick humble").

Off Deezer (its quota, an outage) the caller falls back to MusicBrainz's search: worse ranking,
same shape."""
import asyncio
import logging
import math
import re
import unicodedata
from typing import Optional

import httpx

from ..requests.normalise import normalise
from ..upstream import deezer, lidarr, listenbrainz
from .cache import TTLCache

log = logging.getLogger("continuo")

_SONGS_LOOKED_UP = 10
# Giving the top few artists and albums MusicBrainz IDs (Lidarr) mustn't hold a search up.
_ID_BUDGET_S = 1.4
_ALBUM_POOL = 6
_LOOKUPS = 4
# What doesn't change from one keystroke to the next, kept a day: ListenBrainz allows 30 calls in
# 10 s, and "s", "sa", "sab" find many of the same songs, artists and albums.
_ids_cache = TTLCache(24 * 3600.0, max_items=8192)
_MISS = {}
_WORD = re.compile(r"\w+")
# Deezer's album types: a single named like its song is that song, not an album to open.
_SINGLE = {"single"}


def words(s: Optional[str]) -> list[str]:
    s = unicodedata.normalize("NFKD", (s or "").casefold())
    return _WORD.findall("".join(ch for ch in s if not unicodedata.combining(ch)))


def match(q: str, name: Optional[str]) -> float:
    """How well [name] is what was typed: 3 the same, 2 it starts so, 1.5 every word typed starts
    one of its words ("tyler creator"), 1 it's somewhere inside, 0 not at all."""
    qn, nn = normalise(q), normalise(name)
    if not qn or not nn:
        return 0.0
    if qn == nn:
        return 3.0
    if nn.startswith(qn):
        return 2.0
    typed, has = words(q), words(name)
    if typed and all(any(w.startswith(t) for w in has) for t in typed):
        return 1.5
    return 1.0 if qn in nn else 0.0


def _lead(credit: Optional[str]) -> str:
    """The first artist of a credit, normalised: "Drake feat. Future" -> drake."""
    return normalise(re.split(r"\s+(?:feat\.?|ft\.?|featuring|with|x|&)\s+|,\s*", credit or "", maxsplit=1)[0])


def _same_song(deezer_title: str, deezer_artist: str, found: dict) -> bool:
    """ListenBrainz's lookup is loose: keep its answer only when title and artist agree."""
    a, b = normalise(re.sub(r"\s*[(\[].*?[)\]]", "", deezer_title)), normalise(re.sub(r"\s*[(\[].*?[)\]]", "", found.get("recordingName") or ""))
    if not a or not b or not (a in b or b in a):
        return False
    x, y = _lead(deezer_artist), normalise(found.get("artistCreditName"))
    return bool(x and y and (x in y or y in x))


def _caa(release_mbid: Optional[str]) -> Optional[str]:
    return f"https://coverartarchive.org/release/{release_mbid}/front-500" if release_mbid else None


async def _songs(tracks: list[dict]) -> list[dict]:
    seen, picked = set(), []
    for t in tracks:
        k = (normalise(t["title"]), _lead(t["artist"]))
        if k not in seen:
            seen.add(k)
            picked.append(t)
        if len(picked) >= _SONGS_LOOKED_UP:
            break
    found = await _cached_each("song", [(t["artist"], t["title"]) for t in picked],
                               lambda keys: listenbrainz.lookup_recordings(keys))
    pairs = [(t, f) for t, f in zip(picked, found, strict=True) if f and _same_song(t["title"], t["artist"], f)]
    rec_ids = [f["recordingMbid"] for _, f in pairs]
    rels = await _cached_each("release", rec_ids, lambda keys: _in_order(listenbrainz.recording_releases(keys), keys))
    releases = dict(zip(rec_ids, rels, strict=True))
    out = []
    for t, f in pairs:
        rel = releases.get(f["recordingMbid"]) or {}
        out.append({"kind": "track", "musicBrainzRecordingId": f["recordingMbid"], "title": t["title"],
                    "artist": f.get("artistCreditName") or t["artist"], "durationMs": t["durationMs"],
                    "album": {"title": rel.get("name") or f.get("releaseName") or t.get("album"),
                              "musicBrainzReleaseGroupId": rel.get("releaseGroupMbid"), "year": rel.get("year"),
                              "albumType": None},
                    "imageUrl": t.get("cover") or _caa(rel.get("caaReleaseMbid")),
                    "_deezer": (normalise(t["title"]), _lead(t["artist"]))})
    return out


async def _in_order(coro, keys: list) -> list:
    got = await coro
    return [got.get(k) for k in keys]


async def _cached_each(kind: str, keys: list, fetch) -> list:
    """[fetch]'s answers for [keys] (a list in, a list out, in order), from _ids_cache where it
    has them; only the rest are asked, in one call. Misses are remembered as misses."""
    out = [_ids_cache.get((kind, _key(k))) for k in keys]
    todo = [k for k, v in zip(keys, out, strict=True) if v is None]
    if todo:
        got = await fetch(todo)
        fresh = dict(zip([_key(k) for k in todo], got, strict=True))
        for k, v in fresh.items():
            _ids_cache.put((kind, k), v if v is not None else _MISS)
        out = [v if v is not None else fresh.get(_key(k)) for k, v in zip(keys, out, strict=True)]
    return [None if v is _MISS or v == {} else v for v in out]


def _key(k):
    return tuple(normalise(x) for x in k) if isinstance(k, tuple) else k


async def _cached_one(kind: str, key, fetch):
    hit = _ids_cache.get((kind, _key(key)))
    if hit is not None:
        return None if hit == {} else hit
    value = await fetch()
    _ids_cache.put((kind, _key(key)), value if value is not None else _MISS)
    return value


async def _within(coro, fallback=None):
    try:
        return await asyncio.wait_for(coro, _ID_BUDGET_S)
    except (asyncio.TimeoutError, httpx.HTTPError, ValueError):
        return fallback


async def _artist_mbid(name: str) -> Optional[str]:
    hits = [h for h in await lidarr.search_artists(name) if h.get("musicBrainzArtistId")]
    exact = next((h for h in hits if _exact(h.get("name")) == _exact(name)), None)
    loose = next((h for h in hits if normalise(h.get("name")) == normalise(name)), None)
    return (exact or loose or {}).get("musicBrainzArtistId")


async def _album_ids(title: str, artist: str) -> Optional[dict]:
    want, by = normalise(title), _lead(artist)
    for h in await lidarr.search_albums(f"{artist} {title}"):
        if normalise(h.get("title")) == want and _lead(h.get("artist")) == by and h.get("musicBrainzReleaseGroupId"):
            return {"rg": h["musicBrainzReleaseGroupId"], "artist": h.get("musicBrainzArtistId"),
                    "year": h.get("year"), "type": h.get("albumType")}
    return None


def song_match(q: str, title: str, artist: str) -> float:
    """A song's match: by its title, or by title and artist together ("kendrick humble"), but not
    by its artist's name alone ("chappell" is Chappell Roan, not her top song)."""
    own = match(q, title)
    if own or normalise(artist).startswith(normalise(q)) or match(q, artist) >= 1.5:
        return own
    return max(own, match(q, f"{title} {artist}") - 0.25, match(q, f"{artist} {title}") - 0.25)


async def _listens(rgs: list[str]) -> dict[str, dict]:
    got = await listenbrainz.release_group_popularity(rgs)
    return {rg: {"n": got.get(rg, 0)} for rg in rgs} if got else {}


def _credit_lead_name(credit: str) -> str:
    return re.split(r"\s+(?:feat\.?|ft\.?|featuring|with|x|&)\s+|,\s*", credit, maxsplit=1)[0]


def _exact(name: Optional[str]) -> str:
    """A name as itself, case and accents aside ("The Weeknd" isn't the band "Weeknd")."""
    return " ".join(words(name))


def pools(q: str, tracks: list[dict], raw_artists: list[dict], raw_albums: list[dict]) -> tuple[list[dict], list[dict]]:
    """The artists and albums worth giving IDs: Deezer's artist and album searches, and the top
    songs' artists and albums whose names match what was typed ("taylor": Taylor Swift, whom
    Deezer's artist search puts below a dozen exact Taylors)."""
    artists: dict[str, dict] = {}
    for a in raw_artists:
        artists.setdefault(_exact(a["name"]), {"name": a["name"], "picture": a.get("picture"), "fans": a["fans"]})
    for t in tracks[:10]:
        name = _credit_lead_name(t["artist"])
        if match(q, name) > 0:
            got = artists.setdefault(_exact(name), {"name": name, "picture": None, "fans": None, "rank": 0})
            got["rank"] = max(got.get("rank") or 0, t["rank"])
    albums: dict[tuple[str, str], dict] = {}
    for b in raw_albums[:_ALBUM_POOL]:
        albums.setdefault((normalise(b["title"]), _lead(b["artist"])), dict(b))
    for t in tracks[:10]:
        if t.get("album") and match(q, t["album"]) > 0:
            albums.setdefault((normalise(t["album"]), _lead(t["artist"])),
                              {"title": t["album"], "artist": t["artist"], "recordType": None, "cover": t.get("cover")})
    return list(artists.values()), list(albums.values())


# Popularity on one rough scale, log10 of listens: Deezer's fans for artists, ListenBrainz's
# listens for albums, Deezer's rank (0-1,000,000, already log-like) for songs.
def _song_pop(rank: int) -> float:
    return 6.5 * rank / 1_000_000


def _artist_pop(a: dict) -> float:
    return math.log10(a["fans"] + 1) if a.get("fans") is not None else _song_pop(a.get("rank") or 0) + 0.5


def score(m: float, pop: float) -> float:
    """How strongly a hit leads: how well its name is what was typed, then how popular it is
    (half weight, so popularity orders hits of a kind of match and only overturns a closer match
    for a far bigger name: "sabrina" is Sabrina Carpenter, not the artist called Sabrina)."""
    return m + 0.5 * pop if m > 0 else 0.0


async def catalog(q: str, limit: int) -> dict:
    """{artists, albums, tracks, best} for [q], in the shapes merge_* take; best is a hit key or
    None. Raises deezer.Exhausted or httpx/Deezer errors: the caller falls back."""
    tracks, raw_artists, raw_albums = await asyncio.gather(
        deezer.search_tracks(q, 25), deezer.search_artists(q, 10), deezer.search_albums(q, 10))
    artist_pool, album_pool = pools(q, tracks, raw_artists, raw_albums)
    # Only the likeliest few get looked up: Lidarr takes its lookups more or less one at a time.
    artist_pool = sorted(artist_pool, key=lambda a: -score(match(q, a["name"]) or 0.5, _artist_pop(a)))[:_LOOKUPS]
    album_pool = sorted(album_pool, key=lambda b: -match(q, b["title"]))[:_LOOKUPS]
    # IDs, all at once: the songs' through ListenBrainz, artists' and albums' through Lidarr,
    # each within a budget (a hit without IDs is left out).
    songs, artist_ids, album_ids = await asyncio.gather(
        _within(_songs(tracks), []),
        asyncio.gather(*(_within(_cached_one("artist", _exact(a["name"]), lambda a=a: _artist_mbid(a["name"])))
                         for a in artist_pool)),
        asyncio.gather(*(_within(_cached_one("album", (b["title"], b["artist"]), lambda b=b: _album_ids(b["title"], b["artist"])))
                         for b in album_pool)))
    artists = [dict(a, mbid=m) for a, m in zip(artist_pool, artist_ids, strict=True) if m]
    albums = [dict(b, ids=i) for b, i in zip(album_pool, album_ids, strict=True) if i]
    rgs = [b["ids"]["rg"] for b in albums]
    pops = await _cached_each("rg-listens", rgs, lambda keys: _in_order(_listens(keys), keys))
    b_pop = {rg: p["n"] for rg, p in zip(rgs, pops, strict=True) if p}
    rank_of = {(normalise(t["title"]), _lead(t["artist"])): t["rank"] for t in tracks}
    # Nothing named like what was typed (a typo: "beyonse"): Deezer's own matches count a little.
    anything = any(match(q, a["name"]) for a in artists) or any(match(q, b["title"]) for b in albums) \
        or any(song_match(q, s["title"], s["artist"]) for s in songs)
    floor = 0.0 if anything else 1.0
    for a in artists:
        a["lead"] = score(max(match(q, a["name"]), floor if a.get("fans") is not None else 0.0), _artist_pop(a))
    song_names = {(normalise(s["title"]), _lead(s["artist"])) for s in songs[:3]}
    for b in albums:
        single = (b.get("recordType") or "") in _SINGLE or (b.get("ids") or {}).get("type") == "Single"
        named_like_a_song = (normalise(b["title"]), _lead(b["artist"])) in song_names
        # A single named like its song is that song: the song leads.
        b["lead"] = 0.0 if single and named_like_a_song else \
            score(max(match(q, b["title"]), floor), math.log10(b_pop.get(b["ids"]["rg"], 0) + 1))
    for s in songs:
        m = max(song_match(q, s["title"], s["artist"]), floor)
        s["_lead"] = score(m, _song_pop(rank_of.get(s["_deezer"], 0)))
    artists.sort(key=lambda a: -a["lead"])
    albums.sort(key=lambda b: -b["lead"])
    leads = ([(a["lead"], f"mb:artist:{a['mbid']}") for a in artists[:1]]
             + [(b["lead"], f"mb:rg:{b['ids']['rg']}") for b in albums[:1]]
             + [(s["_lead"], f"mb:rec:{s['musicBrainzRecordingId']}") for s in songs])
    top = max(leads, default=None)
    out_artists = [{"kind": "artist", "name": a["name"], "musicBrainzArtistId": a["mbid"], "imageUrl": a.get("picture"),
                    "lidarrArtistId": None} for a in artists[:limit]]
    out_albums = [{"kind": "album", "title": b["title"], "artist": b["artist"], "year": b["ids"]["year"],
                   "albumType": b["ids"]["type"] or (b.get("recordType") or "").capitalize() or None,
                   "imageUrl": b.get("cover"), "musicBrainzReleaseGroupId": b["ids"]["rg"],
                   "musicBrainzArtistId": b["ids"]["artist"], "releaseIds": [], "owned": False, "trackCount": None,
                   "lidarrAlbumId": None} for b in albums[:limit]]
    for s in songs:
        s.pop("_deezer", None)
        s.pop("_lead", None)
    return {"artists": out_artists, "albums": out_albums, "tracks": songs[:limit],
            "best": top[1] if top and top[0] > 0 else None}
