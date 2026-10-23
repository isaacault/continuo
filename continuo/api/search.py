"""Unified search and catalogue browsing (PLAN.md §3.2)."""
import re

from fastapi import APIRouter, Depends, Query, Request

from .. import artwork
from ..auth.deps import CurrentUser, require_user
from ..db import identity
from ..db import library as lib_db
from ..db import requests as req_db
from ..errors import ApiError
from ..ratelimit import catalog_limiter, search_limiter
from ..requests.normalise import album_key
from ..search import album_match, artist_info, merge, resolve
from ..upstream import lidarr, musicbrainz

router = APIRouter(prefix="/v1", tags=["search"])

_MBID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _check_mbid(mbid: str) -> str:
    if not _MBID.match(mbid):
        raise ApiError(400, "invalid_mbid", "That isn't a MusicBrainz ID.")
    return mbid.lower()


@router.get("/search")
async def search(request: Request, q: str = Query(..., min_length=1, max_length=200),
                 types: str = Query("album,artist,track"),
                 limit: int = Query(25, ge=1, le=50),
                 user: CurrentUser = Depends(require_user)) -> dict:
    search_limiter.check(request)
    wanted = tuple(t for t in (x.strip() for x in types.split(",")) if t in merge.ALL_TYPES)
    if not wanted:
        raise ApiError(400, "invalid_types", "types must be some of album,artist,track.")
    return await merge.unified_search(q.strip(), wanted, limit, user.navidrome_username)


def _album_availability(album: dict, index: merge._LibraryIndex, requests: dict) -> dict:
    rg = album.get("musicBrainzReleaseGroupId")
    lib = index.find(rg, album.get("releaseIds", []), album_key(album.get("artist"), album.get("title")),
                     album.get("title"), album.get("musicBrainzArtistId"))
    if lib is not None:
        return {"state": "in_library", "navidromeAlbumId": lib["navidrome_id"],
                "trackCount": lib.get("song_count")}
    if rg in requests:
        return merge._request_availability(requests[rg])
    if album.get("owned"):
        return {"state": "importing"}
    return {"state": "requestable"}


def _album_out(album: dict, availability: dict) -> dict:
    return {
        "kind": "album", "key": f"mb:rg:{album['musicBrainzReleaseGroupId']}",
        "title": album.get("title"), "artist": album.get("artist"), "year": album.get("year"),
        "albumType": album.get("albumType"),
        "artwork": artwork.catalog_art(album.get("imageUrl")),
        "ids": {"musicBrainzReleaseGroupId": album.get("musicBrainzReleaseGroupId"),
                "musicBrainzArtistId": album.get("musicBrainzArtistId"),
                "lidarrAlbumId": album.get("lidarrAlbumId"),
                "navidromeAlbumId": availability.get("navidromeAlbumId")},
        "availability": availability,
    }


@router.get("/catalog/artists/{mbid}/albums")
async def artist_albums(request: Request, mbid: str,
                        _user: CurrentUser = Depends(require_user)) -> list[dict]:
    catalog_limiter.check(request)
    mbid = _check_mbid(mbid)
    albums = await lidarr.artist_albums(mbid)
    # Untagged library albums by this artist, under either name, matched by release title first.
    names = {albums[0].get("artist") if albums else None, identity.library_name(mbid)}
    await album_match.match_artist(mbid, {n for n in names if n}, albums)
    index = merge._LibraryIndex(lib_db.all_albums())
    requests = req_db.active_by_release_group()
    return [_album_out(a, _album_availability(a, index, requests)) for a in albums]


@router.get("/catalog/artists/resolve")
async def resolve_artist(request: Request, name: str = Query(..., min_length=1, max_length=200),
                         _user: CurrentUser = Depends(require_user)) -> dict:
    """The catalogue artist a library artist without a MusicBrainz id is: same name, and
    among same-name artists the one in Lidarr or sharing the most albums with the library."""
    catalog_limiter.check(request)
    found = await resolve.resolve(name.strip())
    if found is None:
        raise ApiError(404, "not_found", "No artist of that name in the catalogue.")
    return found


@router.get("/catalog/artists/{mbid}")
async def catalog_artist(request: Request, mbid: str,
                         _user: CurrentUser = Depends(require_user)) -> dict:
    """An artist's page for any artist: name, picture, biography, top songs, similar artists."""
    catalog_limiter.check(request)
    page = await artist_info.build(_check_mbid(mbid))
    if page is None:
        raise ApiError(404, "not_found", "Artist not found in the catalogue.")
    return page


@router.get("/catalog/albums/{mbid}")
async def catalog_album(request: Request, mbid: str,
                        _user: CurrentUser = Depends(require_user)) -> dict:
    catalog_limiter.check(request)
    info = await lidarr.lookup_album_by_id(_check_mbid(mbid))
    if not info:
        raise ApiError(404, "not_found", "Album not found in the catalogue.")
    album = lidarr.normalize_album(info)
    index = merge._LibraryIndex(lib_db.all_albums())
    out = _album_out(album, _album_availability(album, index, req_db.active_by_release_group()))
    out["tracks"] = await musicbrainz.album_tracks(mbid)
    return out


@router.get("/library/albums/by-mbid/{mbid}")
async def library_by_mbid(mbid: str, _user: CurrentUser = Depends(require_user)) -> dict:
    """Resolve a release-group or release MBID to the Navidrome album."""
    mbid = _check_mbid(mbid)
    hits = lib_db.by_release_group(mbid) or lib_db.by_release_ids([mbid])
    if not hits:
        raise ApiError(404, "not_in_library", "That album isn't in the library.")
    return {"navidromeAlbumId": hits[0]["navidrome_id"], "name": hits[0]["name"],
            "artist": hits[0]["artist"],
            "alternatives": [h["navidrome_id"] for h in hits[1:]]}
