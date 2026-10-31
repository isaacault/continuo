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
    """A TMDB result as continuo's: {kind, tmdbId, title, year, overview, posterPath, popularity}."""
    kind = {"movie": "movie", "tv": "series"}.get(media_type or hit.get("media_type") or "")
    if kind is None:
        return None
    return {"kind": kind, "tmdbId": hit["id"], "title": hit.get("title") or hit.get("name") or "",
            "year": _year(hit.get("release_date") or hit.get("first_air_date")),
            "overview": hit.get("overview") or None, "posterPath": hit.get("poster_path"),
            "backdropPath": hit.get("backdrop_path"), "popularity": hit.get("popularity") or 0}


async def search(query: str) -> list[dict]:
    body = await _get("/search/multi", {"query": query, "include_adult": "false",
                                        "region": config.settings.tmdb_region})
    return [n for n in (normalize(h) for h in body.get("results") or []) if n]


async def movie(tmdb_id: int) -> dict:
    return await _get(f"/movie/{tmdb_id}", {"append_to_response": "videos,release_dates"})


async def series(tmdb_id: int) -> dict:
    return await _get(f"/tv/{tmdb_id}", {"append_to_response": "external_ids"})


async def season(tmdb_id: int, number: int) -> dict:
    return await _get(f"/tv/{tmdb_id}/season/{number}")
