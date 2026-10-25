"""Navidrome, two ways in:

* Subsonic `/rest/*` (search3, getAlbum, startScan, getScanStatus).
* The native `/api/*` (album index with mbzReleaseGroupId; song paths) — what matching needs.

Auth (NAVIDROME_AUTH):
* "header": continuo's address is in ND_EXTAUTH_TRUSTEDSOURCES, so every call just carries
  `Remote-User: <name>` — as the requesting user for searches, as the service user otherwise.
  Navidrome then ignores u/t/s, and continuo holds no Navidrome password.
* "password": the Phase-1 crutch before ExtAuth is enabled: the service user's password, Subsonic
  token auth for /rest and a /auth/login JWT for /api. Searches then run as the service user.
"""
import hashlib
import logging
import posixpath
import secrets
from typing import Optional

import httpx

from .. import __version__, config
from . import http

log = logging.getLogger("continuo")

_CLIENT_NAME = "continuo"
_API_VERSION = "1.16.1"


class NavidromeError(Exception):
    def __init__(self, code: int | None, message: str):
        super().__init__(message)
        self.code = code


def _client() -> httpx.AsyncClient:
    return http.client("navidrome", base_url=config.settings.navidrome_url, timeout=20.0,
                       headers={"User-Agent": f"continuo/{__version__}"})


def header_mode() -> bool:
    return config.settings.navidrome_auth == "header"


def _subsonic_auth(as_user: Optional[str]) -> tuple[dict, dict]:
    """(params, headers) authenticating a /rest call."""
    s = config.settings
    params = {"v": _API_VERSION, "c": _CLIENT_NAME, "f": "json"}
    if header_mode():
        return params, {s.navidrome_user_header: as_user or s.navidrome_service_user}
    salt = secrets.token_hex(6)
    token = hashlib.md5((s.navidrome_service_password + salt).encode()).hexdigest()
    params.update({"u": s.navidrome_service_user, "t": token, "s": salt})
    return params, {}


async def subsonic(endpoint: str, params: Optional[dict] = None,
                   as_user: Optional[str] = None) -> dict:
    """Call /rest/<endpoint> and return the inner subsonic-response. Raises NavidromeError on
    a Subsonic-level failure, httpx.HTTPError on transport failure."""
    auth_params, headers = _subsonic_auth(as_user)
    r = await _client().get(f"/rest/{endpoint}", params={**auth_params, **(params or {})},
                            headers=headers)
    r.raise_for_status()
    body = r.json().get("subsonic-response", {})
    if body.get("status") != "ok":
        err = body.get("error") or {}
        raise NavidromeError(err.get("code"), err.get("message") or "Navidrome error")
    return body


async def open_subsonic_extensions() -> dict:
    """Navidrome's getOpenSubsonicExtensions answer (the inner subsonic-response), fetched
    without credentials: the endpoint is public by spec, and the façade's copy must not depend
    on any user. Raises httpx.HTTPError / ValueError when Navidrome can't answer."""
    r = await _client().get("/rest/getOpenSubsonicExtensions",
                            params={"v": _API_VERSION, "c": _CLIENT_NAME, "f": "json"})
    r.raise_for_status()
    body = r.json().get("subsonic-response")
    if not isinstance(body, dict):
        raise ValueError("no subsonic-response in Navidrome's answer")
    return body


async def search3(query: str, as_user: Optional[str], *, artists: int = 10, albums: int = 20,
                  songs: int = 20) -> dict:
    body = await subsonic("search3", {
        "query": query, "artistCount": artists, "albumCount": albums, "songCount": songs,
    }, as_user=as_user if header_mode() else None)
    res = body.get("searchResult3") or {}
    return {"artist": res.get("artist") or [], "album": res.get("album") or [],
            "song": res.get("song") or []}


async def get_album(album_id: str, as_user: Optional[str] = None) -> Optional[dict]:
    try:
        body = await subsonic("getAlbum", {"id": album_id}, as_user=as_user)
    except NavidromeError as e:
        if e.code == 70:            # not found
            return None
        raise
    return body.get("album")


async def start_scan(full: bool = False) -> dict:
    body = await subsonic("startScan", {"fullScan": "true" if full else "false"})
    return body.get("scanStatus") or {}


async def scan_status() -> dict:
    body = await subsonic("getScanStatus")
    return body.get("scanStatus") or {}


async def ping(as_user: Optional[str] = None) -> bool:
    try:
        await subsonic("ping", as_user=as_user)
        return True
    except (NavidromeError, httpx.HTTPError, ValueError):
        return False


# --------------------------------------------------------------------------
# Native API
# --------------------------------------------------------------------------
_native_token: Optional[str] = None


async def _native_login() -> str:
    s = config.settings
    if not s.navidrome_service_password:
        raise NavidromeError(None, "native API needs NAVIDROME_SERVICE_PASSWORD or header auth")
    r = await _client().post("/auth/login", json={
        "username": s.navidrome_service_user, "password": s.navidrome_service_password,
    })
    r.raise_for_status()
    token = r.json().get("token")
    if not token:
        raise NavidromeError(None, "Navidrome login returned no token")
    return token


async def native_get(path: str, params: Optional[dict] = None,
                     as_user: Optional[str] = None) -> tuple[list | dict, int | None]:
    """GET /api/<path> as the service user, or (header auth only) as [as_user], whose ratings and
    stars the answer then carries. Returns (json, X-Total-Count)."""
    global _native_token
    s = config.settings
    if as_user and not header_mode():
        raise NavidromeError(None, "acting as another user needs header auth")
    for attempt in (1, 2):
        if header_mode():
            headers = {s.navidrome_user_header: as_user or s.navidrome_service_user}
        else:
            if _native_token is None:
                _native_token = await _native_login()
            headers = {"x-nd-authorization": f"Bearer {_native_token}"}
        r = await _client().get(f"/api/{path}", params=params or {}, headers=headers)
        if r.status_code == 401 and not header_mode() and attempt == 1:
            _native_token = None        # expired session: log in again once
            continue
        r.raise_for_status()
        total = r.headers.get("x-total-count")
        return r.json(), int(total) if total and total.isdigit() else None
    raise NavidromeError(None, "unreachable")


async def native_send(method: str, path: str, body: dict | list) -> dict | list | None:
    """POST/PUT /api/<path> as the service user (an admin): library management (#27)."""
    global _native_token
    s = config.settings
    for attempt in (1, 2):
        if header_mode():
            headers = {s.navidrome_user_header: s.navidrome_service_user}
        else:
            if _native_token is None:
                _native_token = await _native_login()
            headers = {"x-nd-authorization": f"Bearer {_native_token}"}
        r = await _client().request(method, f"/api/{path}", json=body, headers=headers)
        if r.status_code == 401 and not header_mode() and attempt == 1:
            _native_token = None
            continue
        r.raise_for_status()
        return r.json() if r.content else None
    raise NavidromeError(None, "unreachable")


async def iter_albums(page_size: int = 500):
    """Every album via the native API, in pages."""
    start = 0
    while True:
        data, total = await native_get("album", {
            "_start": start, "_end": start + page_size, "_sort": "name", "_order": "ASC",
        })
        if not data:
            return
        for a in data:
            yield a
        start += len(data)
        if len(data) < page_size or (total is not None and start >= total):
            return


def album_folder(paths: list[str]) -> Optional[str]:
    """An album's folder from its songs' (relative) paths: their folder, or for a multi-disc
    album in per-disc subfolders (Album/CD1, Album/CD2) the folder above. Anything wider (songs
    spread over unrelated folders) keeps the first song's folder, so an album never stands for
    a whole artist's folder."""
    dirs = [posixpath.dirname(p) for p in paths if p]
    if not dirs:
        return None
    if len(set(dirs)) == 1:
        return dirs[0]
    common = posixpath.commonpath(dirs)
    if common.count("/") >= 1 and all(d == common or posixpath.dirname(d) == common for d in dirs):
        return common
    return dirs[0]


async def album_song_paths(album_id: str, limit: int = 1) -> list[str]:
    """Paths of the album's songs that are on disk: Navidrome keeps a song whose file went (a
    renamed folder) as missing until purged, and that path no longer leads anywhere."""
    data, _ = await native_get("song", {"album_id": album_id, "_start": 0, "_end": limit,
                                        "_sort": "album", "_order": "ASC"})
    return [s.get("path") for s in data if s.get("path") and not s.get("missing")]


def relative_path(path: Optional[str], root: Optional[str] = None) -> str:
    """A path relative to the music root, '/'-separated, no leading slash. Navidrome ≥0.56
    already reports library-relative paths; older ones report absolute container paths."""
    if not path:
        return ""
    root = (root if root is not None else config.settings.navidrome_music_root).rstrip("/")
    p = path.replace("\\", "/")
    if root and (p == root or p.startswith(root + "/")):
        p = p[len(root):]
    return p.lstrip("/")
