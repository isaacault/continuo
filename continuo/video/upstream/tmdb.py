"""TMDB, the video catalogue (as MusicBrainz is music's): search, titles, posters. Answers are
cached on disk (video_tmdb_cache) for a day; a v3 API key goes as api_key, a v4 token as Bearer."""
import json

import httpx

from ... import config
from ...db.conn import connect, now, seconds_since
from ...upstream import http

_BASE = "https://api.themoviedb.org/3"
_TTL = 24 * 3600


class Unavailable(Exception):
    pass


def configured() -> bool:
    return bool(config.settings.tmdb_api_key)


def _client() -> httpx.AsyncClient:
    key = config.settings.tmdb_api_key
    headers = {"Authorization": f"Bearer {key}"} if key.startswith("eyJ") else {}
    return http.client("tmdb", base_url=_BASE, headers=headers, timeout=15.0)


def _params(extra: dict) -> dict:
    s = config.settings
    out = {"language": s.tmdb_language, **extra}
    if not s.tmdb_api_key.startswith("eyJ"):
        out["api_key"] = s.tmdb_api_key
    return out


def _cache_key(path: str, params: dict) -> str:
    return path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "api_key")


async def _get(path: str, params: dict | None = None) -> dict:
    params = _params(params or {})
    key = _cache_key(path, params)
    with connect() as c:
        r = c.execute("SELECT body, fetched_at FROM video_tmdb_cache WHERE key=?", (key,)).fetchone()
    if r and (seconds_since(r[1]) or 0) < _TTL:
        return json.loads(r[0])
    try:
        resp = await _client().get(path, params=params)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        if r:
            return json.loads(r[0])          # stale beats nothing
        raise Unavailable(str(e) or e.__class__.__name__) from e
    with connect() as c:
        c.execute("INSERT INTO video_tmdb_cache (key, body, fetched_at) VALUES (?, ?, ?) ON CONFLICT (key) "
                  "DO UPDATE SET body=excluded.body, fetched_at=excluded.fetched_at",
                  (key, json.dumps(body, separators=(",", ":")), now()))
    return body


async def ping() -> bool:
    try:
        await _get("/configuration")
        return True
    except Unavailable:
        return False


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and date[:4].isdigit() else None


def normalize(hit: dict, media_type: str | None = None) -> dict | None:
    """A TMDB result as continuo's: {kind, tmdbId, title, year, overview, posterPath, backdropPath,
    popularity, genreIds, voteAverage, releaseDate}."""
    kind = {"movie": "movie", "tv": "series"}.get(media_type or hit.get("media_type") or "")
    if kind is None:
        return None
    date = hit.get("release_date") or hit.get("first_air_date") or None
    return {"kind": kind, "tmdbId": hit["id"], "title": hit.get("title") or hit.get("name") or "",
            "year": _year(date), "releaseDate": date,
            "overview": hit.get("overview") or None, "posterPath": hit.get("poster_path"),
            "backdropPath": hit.get("backdrop_path"), "popularity": hit.get("popularity") or 0,
            "genreIds": hit.get("genre_ids") or [], "voteAverage": hit.get("vote_average") or None}


def _results(body: dict, media_type: str | None = None) -> list[dict]:
    return [n for n in (normalize(h, media_type) for h in body.get("results") or []) if n]


def _tv(kind: str) -> str:
    return "movie" if kind == "movie" else "tv"


async def search(query: str) -> list[dict]:
    body = await _get("/search/multi", {"query": query, "include_adult": "false",
                                        "region": config.settings.tmdb_region})
    return [n for n in (normalize(h) for h in body.get("results") or []) if n]


# Title pages: everything a detail screen shows in one call (cast, age ratings, "More Like This",
# the title's logo). Logos in the configured language or without text.
_TITLE_EXTRAS = "videos,credits,recommendations,images"


def _image_languages() -> str:
    return f"{config.settings.tmdb_language.split('-')[0]},null"


async def movie(tmdb_id: int) -> dict:
    return await _get(f"/movie/{tmdb_id}", {"append_to_response": f"{_TITLE_EXTRAS},release_dates",
                                           "include_image_language": _image_languages()})


async def series(tmdb_id: int) -> dict:
    return await _get(f"/tv/{tmdb_id}", {"append_to_response": f"{_TITLE_EXTRAS},external_ids,content_ratings",
                                        "include_image_language": _image_languages()})


async def season(tmdb_id: int, number: int) -> dict:
    return await _get(f"/tv/{tmdb_id}/season/{number}")


async def trending() -> list[dict]:
    """This week's trending films and series (people left out)."""
    return _results(await _get("/trending/all/week"))


async def trending_today(kind: str) -> list[dict]:
    """Today's trending films or series: the Top 10 rows."""
    return _results(await _get(f"/trending/{_tv(kind)}/day"), _tv(kind))


async def popular(kind: str) -> list[dict]:
    """Popular films ("movie") or series ("series") in the configured region."""
    return _results(await _get(f"/{_tv(kind)}/popular", {"region": config.settings.tmdb_region}), _tv(kind))


async def genres(kind: str) -> list[dict]:
    """[{id, name}] for films or series."""
    body = await _get(f"/genre/{_tv(kind)}/list")
    return [{"id": g["id"], "name": g["name"]} for g in body.get("genres") or [] if g.get("id") and g.get("name")]


# How a browse row can be ordered: TMDB's discover sort for each.
SORTS = {"popular": "popularity.desc", "top_rated": "vote_average.desc", "new": "primary_release_date.desc"}


async def discover(kind: str, genre: int | None = None, sort: str = "popular", page: int = 1) -> dict:
    """{results, page, totalPages}: films or series, by genre, ordered. "top_rated" needs enough
    votes to mean something; "new" only what's already out."""
    s = config.settings
    order = SORTS.get(sort, SORTS["popular"])
    if kind != "movie":
        order = order.replace("primary_release_date", "first_air_date")
    params: dict = {"sort_by": order, "page": page, "include_adult": "false", "watch_region": s.tmdb_region}
    if genre:
        params["with_genres"] = genre
    if sort == "top_rated":
        params["vote_count.gte"] = 300
    if sort == "new":
        from datetime import date
        params["primary_release_date.lte" if kind == "movie" else "first_air_date.lte"] = date.today().isoformat()
        params["vote_count.gte"] = 20
    body = await _get(f"/discover/{_tv(kind)}", params)
    return {"results": _results(body, _tv(kind)), "page": body.get("page") or page,
            "totalPages": min(body.get("total_pages") or 1, 500)}


async def upcoming() -> list[dict]:
    """Films coming to cinemas in the configured region."""
    return _results(await _get("/movie/upcoming", {"region": config.settings.tmdb_region}), "movie")


async def on_the_air() -> list[dict]:
    """Series with episodes airing in the coming week."""
    return _results(await _get("/tv/on_the_air"), "tv")


def is_anime(show: dict) -> bool:
    """Anime, as TMDB has it: animation (genre 16) made in Japan (origin country, or Japanese as the
    original language)."""
    genres = {g.get("id") for g in show.get("genres") or []} | set(show.get("genre_ids") or [])
    japanese = "JP" in (show.get("origin_country") or []) or show.get("original_language") == "ja"
    return 16 in genres and japanese
