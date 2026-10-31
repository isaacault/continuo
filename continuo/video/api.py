"""The video part's API (docs/design/video.md §7): search, film pages, film requests. Under
/v1/video; the TV/web/phone clients use it with the same continuo sign-in as music's apps."""
import asyncio

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..artwork import public_url
from ..auth.deps import CurrentUser, require_user
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


class RequestIn(BaseModel):
    kind: str
    tmdbId: int


def _request_out(r: dict) -> dict:
    return {"id": r["id"], "kind": r["kind"], "tmdbId": r["tmdb_id"], "title": r["title"], "year": r["year"],
            "poster": _image(r.get("poster_path")), "status": r["status"], "error": r.get("error"),
            "jellyfinId": r.get("jellyfin_id"), "createdAt": r["created_at"], "availableAt": r.get("available_at")}


@router.get("/requests")
async def list_requests(user: CurrentUser = Depends(require_user)) -> dict:
    return {"requests": [_request_out(r) for r in db.for_user(None if user.role == "admin" else user.id)]}


@router.post("/requests", status_code=201)
async def create_request(body: RequestIn, user: CurrentUser = Depends(require_user)) -> dict:
    if body.kind != "movie":
        raise ApiError(400, "unsupported_kind", "Only films can be requested so far; series are coming.")
    if index.by_tmdb("movie", body.tmdbId):
        raise ApiError(409, "in_library", "That film is already in the library.")
    existing = db.active_for("movie", body.tmdbId)
    if existing:
        return {"request": _request_out(existing), "created": False}
    title = year = poster = None
    if tmdb.configured():
        try:
            m = await tmdb.movie(body.tmdbId)
            title, poster = m.get("title"), m.get("poster_path")
            year = int(m["release_date"][:4]) if (m.get("release_date") or "")[:4].isdigit() else None
        except tmdb.Unavailable:
            pass
    req = db.create(kind="movie", tmdb_id=body.tmdbId, title=title, year=year, poster_path=poster, user_id=user.id)
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
