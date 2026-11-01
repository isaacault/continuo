"""qBittorrent, where Radarr's downloads go: what Play now needs of it. A torrent downloaded in
order (sequential, first and last pieces first) and which of its pieces have arrived, so the part
of a film that's there can be played while the rest comes (docs/design/video.md §9)."""
import asyncio
import logging
import time

import httpx

from ... import config
from ...upstream import http

log = logging.getLogger("continuo")
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
SETTLE_CHECK_SECONDS = 2.0


async def stream_mode(hash_: str, t: dict) -> None:
    """Sequential download, first and last pieces first: what playing it as it comes needs. With
    QBITTORRENT_PLAY_NOW_TAG, the torrent is tagged and the tool that owns that tag switches it (two
    switchers with toggles undo each other: the homelab's watchnow turned it off every 10 s)."""
    tag = config.settings.qbittorrent_play_now_tag
    if tag:
        if tag not in [x.strip() for x in (t.get("tags") or "").split(",")]:
            await _call("POST", "/torrents/addTags", data={"hashes": hash_.lower(), "tags": tag})
            log.info("play now: %s tagged %s (that tool keeps it downloading in order)", hash_.lower()[:12], tag)
        return
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
            # qBittorrent 5 has only toggles: say what was done and whether it stuck, so a setting that
            # comes back off (seen on the homelab) can be traced.
            log.info("play now: %s was in order=%s, first/last first=%s: switched on", hash_.lower()[:12],
                     fresh.get("seq_dl"), fresh.get("f_l_piece_prio"))
            await asyncio.sleep(SETTLE_CHECK_SECONDS)
            after = await torrent(hash_) or {}
            if not (after.get("seq_dl") and after.get("f_l_piece_prio")):
                log.warning("play now: %s didn't stay on (in order=%s, first/last first=%s)", hash_.lower()[:12],
                            after.get("seq_dl"), after.get("f_l_piece_prio"))


# --- Play now's priority: the latest at full speed, the others slowly on --------------------------

DEMOTED_LIMIT = 512 * 1024          # bytes/s for an older Play now while a newer one downloads


async def promote(hash_: str, t: dict) -> None:
    """The Play now being watched: in order, no speed limit, top of the queue."""
    await stream_mode(hash_, t)
    if (t.get("dl_limit") or 0) > 0:
        await _call("POST", "/torrents/setDownloadLimit", data={"hashes": hash_.lower(), "limit": 0})
    await _call("POST", "/torrents/topPrio", data={"hashes": hash_.lower()})


async def demote(hash_: str, t: dict) -> None:
    """An older Play now: back to an ordinary download (out of order), slowed, so the latest gets
    the bandwidth. It carries on into the library."""
    tag = config.settings.qbittorrent_play_now_tag
    if tag and tag in [x.strip() for x in (t.get("tags") or "").split(",")]:
        await _call("POST", "/torrents/removeTags", data={"hashes": hash_.lower(), "tags": tag})
    elif not tag and t.get("seq_dl"):
        await _call("POST", "/torrents/toggleSequentialDownload", data={"hashes": hash_.lower()})
    if (t.get("dl_limit") or 0) != DEMOTED_LIMIT:
        await _call("POST", "/torrents/setDownloadLimit", data={"hashes": hash_.lower(), "limit": DEMOTED_LIMIT})
