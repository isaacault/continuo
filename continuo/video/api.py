"""The video part's API (docs/design/video.md §7): search, film pages, film requests. Under
/v1/video; the TV/web/phone clients use it with the same continuo sign-in as music's apps."""
import asyncio
import json

import httpx
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..artwork import public_url
from ..auth.deps import CurrentUser, require_admin, require_user
from ..errors import ApiError
from . import db, index
from .upstream import tmdb

router = APIRouter(prefix="/v1/video", tags=["video"])

_IMAGE = "https://image.tmdb.org/t/p/{size}{path}"
_tasks: set[asyncio.Task] = set()


def _image(path: str | None, size: str = "w500") -> str | None:
    return public_url(_IMAGE.format(size=size, path=path)) if path else None


def availability(kind: str, tmdb_id: int) -> dict:
    """What a title is to this server: in the library (with its Jellyfin ID), requested, or not."""
    item = index.by_tmdb(kind, tmdb_id)
    if item:
        return {"state": "in_library", "jellyfinId": item["jellyfin_id"]}
    req = db.active_for(kind, tmdb_id)
    if req:
        return {"state": "requested", "requestId": req["id"], "status": req["status"]}
    return {"state": "requestable"}


def _hit(h: dict) -> dict:
    return {"kind": h["kind"], "tmdbId": h["tmdbId"], "title": h["title"], "year": h["year"],
            "overview": h.get("overview"), "poster": _image(h.get("posterPath")),
            "availability": availability(h["kind"], h["tmdbId"])}


@router.get("/search")
async def search(q: str = Query(..., min_length=1, max_length=200), _user: CurrentUser = Depends(require_user)) -> dict:
    """Films and series: TMDB's answer marked with what's in the library and what's requested, then
    library titles TMDB didn't return (home videos, oddly named files: library only)."""
    hits: list[dict] = []
    catalog = "ok"
    if tmdb.configured():
        try:
            hits = [_hit(h) for h in await tmdb.search(q)]
        except tmdb.Unavailable:
            catalog = "unavailable"
    else:
        catalog = "not_configured"
    seen = {(h["kind"], h["tmdbId"]) for h in hits}
    needle = q.casefold()
    library = [{"kind": i["kind"], "tmdbId": i["tmdb_id"], "title": i["title"], "year": i["year"], "overview": None,
                "poster": None, "availability": {"state": "in_library", "jellyfinId": i["jellyfin_id"]}}
               for i in index.all_items()
               if needle in i["title"].casefold() and (i["kind"], i["tmdb_id"]) not in seen]
    return {"query": q, "sources": {"catalog": catalog, "library": "ok"}, "results": hits + library[:20]}


@router.get("/movies/{tmdb_id}")
async def movie(tmdb_id: int, _user: CurrentUser = Depends(require_user)) -> dict:
    if not tmdb.configured():
        raise ApiError(503, "catalog_not_configured", "This server has no TMDB key.")
    try:
        m = await tmdb.movie(tmdb_id)
    except tmdb.Unavailable as e:
        raise ApiError(503, "catalog_unavailable", "TMDB isn't answering right now.", True) from e
    trailer = next((v["key"] for v in (m.get("videos") or {}).get("results") or []
                    if v.get("site") == "YouTube" and v.get("type") == "Trailer"), None)
    return {"kind": "movie", "tmdbId": tmdb_id, "title": m.get("title") or "",
            "year": int(m["release_date"][:4]) if (m.get("release_date") or "")[:4].isdigit() else None,
            "overview": m.get("overview") or None, "runtimeMinutes": m.get("runtime") or None,
            "genres": [g["name"] for g in m.get("genres") or [] if g.get("name")],
            "poster": _image(m.get("poster_path")), "backdrop": _image(m.get("backdrop_path"), "w1280"),
            "trailerYouTubeKey": trailer, "availability": availability("movie", tmdb_id)}


async def _jellyfin_user(user: CurrentUser) -> str | None:
    from . import accounts
    acct = accounts.account(user.id)
    return acct["jellyfin_user_id"] if acct else None


@router.get("/series/{tmdb_id}")
async def series_page(tmdb_id: int, user: CurrentUser = Depends(require_user)) -> dict:
    """A series and its seasons, each with the episodes the library has against TMDB's count: a
    season with fewer is partial (specials, season 0, are listed but never counted)."""
    show = await _tmdb(tmdb.series(tmdb_id))
    item = index.by_tmdb("series", tmdb_id)
    have: dict[int, int] = {}
    if item:
        from .upstream import jellyfin
        try:
            for e in await jellyfin.episodes(item["jellyfin_id"]):
                if e["season"] is not None:
                    have[e["season"]] = have.get(e["season"], 0) + 1
        except httpx.HTTPError:
            pass
    seasons = []
    for x in show.get("seasons") or []:
        n, expected = x.get("season_number"), x.get("episode_count") or 0
        got = have.get(n, 0)
        state = "in_library" if expected and got >= expected else "partial" if got else "missing"
        seasons.append({"number": n, "name": x.get("name"), "episodes": expected, "have": got,
                        "airDate": x.get("air_date"), "poster": _image(x.get("poster_path")), "state": state})
    return {"kind": "series", "tmdbId": tmdb_id, "title": show.get("name") or "",
            "year": int(show["first_air_date"][:4]) if (show.get("first_air_date") or "")[:4].isdigit() else None,
            "overview": show.get("overview") or None, "status": show.get("status"),
            "genres": [g["name"] for g in show.get("genres") or [] if g.get("name")],
            "poster": _image(show.get("poster_path")), "backdrop": _image(show.get("backdrop_path"), "w1280"),
            "seasons": seasons, "availability": availability("series", tmdb_id)}


@router.get("/series/{tmdb_id}/seasons/{number}")
async def season_page(tmdb_id: int, number: int, user: CurrentUser = Depends(require_user)) -> dict:
    """A season's episodes: in the library or not, each with its Jellyfin ID and (for a linked or
    created Jellyfin user) whether it's watched and where it was left."""
    season = await _tmdb(tmdb.season(tmdb_id, number))
    item = index.by_tmdb("series", tmdb_id)
    mine: dict[int, dict] = {}
    if item:
        from .upstream import jellyfin
        try:
            for e in await jellyfin.episodes(item["jellyfin_id"], await _jellyfin_user(user), season=number):
                if e["episode"] is not None:
                    mine[e["episode"]] = e
        except httpx.HTTPError:
            pass
    episodes = []
    for e in season.get("episodes") or []:
        n = e.get("episode_number")
        got = mine.get(n)
        episodes.append({"number": n, "title": e.get("name"), "overview": e.get("overview") or None,
                         "airDate": e.get("air_date"), "runtimeMinutes": e.get("runtime"),
                         "still": _image(e.get("still_path"), "w300"),
                         "inLibrary": got is not None, "jellyfinId": got["jellyfinId"] if got else None,
                         "played": got["played"] if got else False,
                         "positionSeconds": got["positionSeconds"] if got else 0})
    return {"tmdbId": tmdb_id, "number": number, "name": season.get("name"), "episodes": episodes}


async def _tmdb(call):
    if not tmdb.configured():
        raise ApiError(503, "catalog_not_configured", "This server has no TMDB key.")
    try:
        return await call
    except tmdb.Unavailable as e:
        raise ApiError(503, "catalog_unavailable", "TMDB isn't answering right now.", True) from e


class RequestIn(BaseModel):
    kind: str
    tmdbId: int
    # A series: which seasons (none: every aired season but specials).
    seasons: list[int] | None = None


def _request_out(r: dict) -> dict:
    return {"id": r["id"], "kind": r["kind"], "tmdbId": r["tmdb_id"], "title": r["title"], "year": r["year"],
            "poster": _image(r.get("poster_path")), "status": r["status"], "error": r.get("error"),
            "jellyfinId": r.get("jellyfin_id"), "createdAt": r["created_at"], "availableAt": r.get("available_at"),
            "seasons": json.loads(r["seasons"]) if r.get("seasons") else None}


@router.get("/requests")
async def list_requests(user: CurrentUser = Depends(require_user)) -> dict:
    return {"requests": [_request_out(r) for r in db.for_user(None if user.role == "admin" else user.id)]}


@router.post("/requests", status_code=201)
async def create_request(body: RequestIn, user: CurrentUser = Depends(require_user)) -> dict:
    if body.kind not in ("movie", "series"):
        raise ApiError(400, "unsupported_kind", "Films (movie) and series can be requested.")
    if body.kind == "movie" and index.by_tmdb("movie", body.tmdbId):
        raise ApiError(409, "in_library", "That film is already in the library.")
    existing = db.active_for(body.kind, body.tmdbId)
    if existing:
        return {"request": _request_out(existing), "created": False}
    title = year = poster = None
    if tmdb.configured():
        try:
            m = await (tmdb.movie(body.tmdbId) if body.kind == "movie" else tmdb.series(body.tmdbId))
            title, poster = m.get("title") or m.get("name"), m.get("poster_path")
            date = m.get("release_date") or m.get("first_air_date") or ""
            year = int(date[:4]) if date[:4].isdigit() else None
        except tmdb.Unavailable:
            pass
    req = db.create(kind=body.kind, tmdb_id=body.tmdbId, title=title, year=year, poster_path=poster, user_id=user.id,
                    seasons=sorted(set(body.seasons)) if body.kind == "series" and body.seasons else None)
    from . import requests as pipeline
    task = asyncio.get_running_loop().create_task(pipeline.start(req["id"]))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"request": _request_out(req), "created": True}


@router.delete("/requests/{request_id}", status_code=204)
async def delete_request(request_id: int, user: CurrentUser = Depends(require_user)) -> None:
    """Forgets the request (the film stays in Radarr: removing it there is Radarr's own job)."""
    req = db.get(request_id)
    if not req or (req["user_id"] != user.id and user.role != "admin"):
        raise ApiError(404, "not_found", "No such request.")
    db.delete(request_id)


@router.get("/session")
async def video_session(user: CurrentUser = Depends(require_user)) -> dict:
    """This device's Jellyfin session (created on first ask): stream and report progress there."""
    from ..db import devices as devices_db
    from . import accounts
    device = devices_db.get(user.device_id) or {}
    try:
        return await accounts.session(user.id, user.username, user.device_id, device.get("name") or "device")
    except httpx.HTTPError as e:
        raise ApiError(503, "jellyfin_unavailable", "Jellyfin isn't answering right now.", True) from e


admin_router = APIRouter(prefix="/v1/admin/video", tags=["admin"])


class LinkIn(BaseModel):
    userId: int
    jellyfinUsername: str
    jellyfinPassword: str


@admin_router.post("/accounts")
async def link_account(body: LinkIn, _admin: CurrentUser = Depends(require_admin)) -> dict:
    """Links a continuo account to an existing Jellyfin user (so its watch history carries on)."""
    from . import accounts
    acct = await accounts.link(body.userId, body.jellyfinUsername, body.jellyfinPassword)
    return {"userId": acct["user_id"], "jellyfinUserId": acct["jellyfin_user_id"],
            "jellyfinUsername": acct["jellyfin_username"], "managed": bool(acct["managed"])}
