"""Browsing like Netflix: genres, rows by genre, Top 10 today, New & Popular, My List, ratings, and
watched/unwatched (docs/design/video.md §7). Titles come back as the search's hits, each marked with
what's in the library and what's requested."""
import asyncio

import httpx
from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel

from ..auth.deps import CurrentUser, require_user
from ..errors import ApiError
from . import mylist
from .api import _hit, _image, _jellyfin_user, _tmdb, availability
from .upstream import tmdb

router = APIRouter(prefix="/v1/video", tags=["video"])
Kind = Path(..., pattern="^(movie|series)$")


@router.get("/genres")
async def genres(kind: str = Query(..., pattern="^(movie|series)$"), _user: CurrentUser = Depends(require_user)) -> dict:
    return {"kind": kind, "genres": await _tmdb(tmdb.genres(kind))}


@router.get("/browse")
async def browse(kind: str = Query(..., pattern="^(movie|series)$"), genre: int | None = Query(None, ge=1),
                 sort: str = Query("popular", pattern="^(popular|top_rated|new)$"), page: int = Query(1, ge=1, le=500),
                 _user: CurrentUser = Depends(require_user)) -> dict:
    """One row or grid of films or series: by genre, ordered popular, top rated or new; paged."""
    out = await _tmdb(tmdb.discover(kind, genre, sort, page))
    return {"kind": kind, "genre": genre, "sort": sort, "page": out["page"], "totalPages": out["totalPages"],
            "results": [_hit(h) for h in out["results"]]}


@router.get("/top10")
async def top10(kind: str = Query(..., pattern="^(movie|series)$"), _user: CurrentUser = Depends(require_user)) -> dict:
    """Today's ten most-watched films or series (TMDB's trending today), ranked."""
    hits = await _tmdb(tmdb.trending_today(kind))
    return {"kind": kind, "results": [{**_hit(h), "rank": n} for n, h in enumerate(hits[:10], start=1)]}


@router.get("/new")
async def new_and_popular(_user: CurrentUser = Depends(require_user)) -> dict:
    """New & Popular: films coming soon (with their dates), series with new episodes this week, and
    what's trending. A part TMDB can't give is empty."""
    calls = {"comingSoon": tmdb.upcoming(), "newEpisodes": tmdb.on_the_air(), "trending": tmdb.trending()}
    if not tmdb.configured():
        return {name: [] for name in calls} | {"catalog": "not_configured"}
    results = await asyncio.gather(*calls.values(), return_exceptions=True)
    out = {name: [] if isinstance(r, BaseException) else [_hit(h) for h in r[:20]]
           for name, r in zip(calls, results, strict=True)}
    out["comingSoon"].sort(key=lambda h: h.get("releaseDate") or "9999")
    return {**out, "catalog": "ok"}


# --- My List ---------------------------------------------------------------------------------------

def _entry_out(e: dict) -> dict:
    return {"kind": e["kind"], "tmdbId": e["tmdb_id"], "title": e["title"], "year": e["year"],
            "poster": _image(e["poster_path"]), "backdrop": _image(e["backdrop_path"], "w780"),
            "addedAt": e["added_at"], "availability": availability(e["kind"], e["tmdb_id"])}


@router.get("/my-list")
async def my_list(user: CurrentUser = Depends(require_user)) -> dict:
    return {"results": [_entry_out(e) for e in mylist.entries(user.id)]}


async def _title(kind: str, tmdb_id: int) -> dict:
    body = await _tmdb(tmdb.movie(tmdb_id) if kind == "movie" else tmdb.series(tmdb_id))
    if not body.get("id"):
        raise ApiError(404, "not_found", "TMDB doesn't know that title.")
    return body


@router.put("/my-list/{kind}/{tmdb_id}")
async def add_to_my_list(kind: str = Kind, tmdb_id: int = Path(..., ge=1), user: CurrentUser = Depends(require_user)) -> dict:
    if not mylist.contains(user.id, kind, tmdb_id):
        t = await _title(kind, tmdb_id)
        date = t.get("release_date") or t.get("first_air_date") or ""
        mylist.add(user.id, kind, tmdb_id, title=t.get("title") or t.get("name") or "",
                   year=int(date[:4]) if date[:4].isdigit() else None,
                   poster_path=t.get("poster_path"), backdrop_path=t.get("backdrop_path"))
    return {"inMyList": True}


@router.delete("/my-list/{kind}/{tmdb_id}")
async def remove_from_my_list(kind: str = Kind, tmdb_id: int = Path(..., ge=1), user: CurrentUser = Depends(require_user)) -> dict:
    mylist.remove(user.id, kind, tmdb_id)
    return {"inMyList": False}


# --- ratings ---------------------------------------------------------------------------------------

class RatingIn(BaseModel):
    rating: int        # -1 not for me, 1 like, 2 love, 0 clear


@router.put("/ratings/{kind}/{tmdb_id}")
async def rate(body: RatingIn, kind: str = Kind, tmdb_id: int = Path(..., ge=1), user: CurrentUser = Depends(require_user)) -> dict:
    if body.rating not in (-1, 0, 1, 2):
        raise ApiError(400, "bad_rating", "A rating is -1 (not for me), 1 (like), 2 (love) or 0 (clear).")
    title = ""
    if body.rating:
        t = await _title(kind, tmdb_id)
        title = t.get("title") or t.get("name") or ""
    mylist.rate(user.id, kind, tmdb_id, body.rating, title)
    return {"myRating": body.rating}


@router.get("/because-you-liked")
async def because_you_liked(user: CurrentUser = Depends(require_user)) -> dict:
    """Rows of "Because you liked X": TMDB's recommendations for the titles this account liked last."""
    rows = []
    for r in mylist.liked(user.id, 3):
        try:
            body = await (tmdb.movie(r["tmdb_id"]) if r["kind"] == "movie" else tmdb.series(r["tmdb_id"]))
        except tmdb.Unavailable:
            continue
        recs = [tmdb.normalize(h, "movie" if r["kind"] == "movie" else "tv")
                for h in (body.get("recommendations") or {}).get("results") or []]
        hits = [_hit(h) for h in recs if h][:18]
        if hits:
            rows.append({"because": {"kind": r["kind"], "tmdbId": r["tmdb_id"], "title": r["title"]}, "results": hits})
    return {"rows": rows}


# --- watched / not watched (Jellyfin's, for this account's Jellyfin user) ---------------------------

class PlayedIn(BaseModel):
    played: bool


@router.put("/played/{jellyfin_id}")
async def set_played(body: PlayedIn, jellyfin_id: str = Path(..., pattern="^[A-Za-z0-9-]{1,64}$"),
                     user: CurrentUser = Depends(require_user)) -> dict:
    """A film, an episode, a season or a whole series, marked watched or not."""
    from .upstream import jellyfin
    jf_user = await _jellyfin_user(user)
    if not jf_user:
        raise ApiError(409, "jellyfin_link_needed", "This account has no Jellyfin user yet.")
    try:
        await jellyfin.set_played(jf_user, jellyfin_id, body.played)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise ApiError(404, "not_found", "Jellyfin doesn't have that.") from e
        raise ApiError(502, "jellyfin_unavailable", "Jellyfin didn't take it.", True) from e
    except httpx.HTTPError as e:
        raise ApiError(502, "jellyfin_unavailable", "Jellyfin isn't answering.", True) from e
    return {"played": body.played}
