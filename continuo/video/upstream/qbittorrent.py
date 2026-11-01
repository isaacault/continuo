"""qBittorrent, where Radarr's downloads go: what Play now needs of it. A torrent downloaded in
order (sequential, first and last pieces first) and which of its pieces have arrived, so the part
of a film that's there can be played while the rest comes (docs/design/video.md §9)."""
import asyncio
import time

import httpx

from ... import config
from ...upstream import http

VIDEO_EXTENSIONS = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts")


def configured() -> bool:
    return bool(config.settings.qbittorrent_url)


def _client() -> httpx.AsyncClient:
    s = config.settings
    # qBittorrent checks Referer (or Origin) against its own address on every call.
    return http.client("qbittorrent", base_url=f"{s.qbittorrent_url}/api/v2", headers={"Referer": s.qbittorrent_url},
                       timeout=15.0)


async def _login() -> None:
    s = config.settings
    r = await _client().post("/auth/login", data={"username": s.qbittorrent_username, "password": s.qbittorrent_password})
    r.raise_for_status()
    if r.text.strip() == "Fails.":
        raise httpx.HTTPStatusError("qBittorrent refused the username or password", request=r.request, response=r)


async def _call(method: str, path: str, **kw) -> httpx.Response:
    """A call, signing in first when qBittorrent asks (403) and the account has credentials."""
    r = await _client().request(method, path, **kw)
    if r.status_code == 403 and config.settings.qbittorrent_username:
        await _login()
        r = await _client().request(method, path, **kw)
    r.raise_for_status()
    return r


async def ping() -> bool:
    try:
        await _call("GET", "/app/version")
        return True
    except httpx.HTTPError:
        return False


async def torrent(hash_: str) -> dict | None:
    r = await _call("GET", "/torrents/info", params={"hashes": hash_.lower()})
    found = r.json()
    return found[0] if found else None


async def files(hash_: str) -> list[dict]:
    return (await _call("GET", "/torrents/files", params={"hash": hash_.lower()})).json()


async def piece_states(hash_: str) -> list[int]:
    """Per piece: 0 not there, 1 coming, 2 here."""
    return (await _call("GET", "/torrents/pieceStates", params={"hash": hash_.lower()})).json()


# Both settings are toggles in qBittorrent's API: two checks at once each flipping one turned it back
# off (seen on the homelab). One at a time, read fresh, and not again while a flip may not show yet.
_mode_lock = asyncio.Lock()
_flipped: dict[str, float] = {}
FLIP_SETTLE_SECONDS = 30


async def stream_mode(hash_: str, t: dict) -> None:
    """Sequential download, first and last pieces first: what playing it as it comes needs."""
    if t.get("seq_dl") and t.get("f_l_piece_prio"):
        return
    async with _mode_lock:
        if time.monotonic() - _flipped.get(hash_.lower(), -FLIP_SETTLE_SECONDS) < FLIP_SETTLE_SECONDS:
            return
        fresh = await torrent(hash_) or t
        flipped = False
        if not fresh.get("seq_dl"):
            await _call("POST", "/torrents/toggleSequentialDownload", data={"hashes": hash_.lower()})
            flipped = True
        if not fresh.get("f_l_piece_prio"):
            await _call("POST", "/torrents/toggleFirstLastPiecePrio", data={"hashes": hash_.lower()})
            flipped = True
        if flipped:
            _flipped[hash_.lower()] = time.monotonic()
