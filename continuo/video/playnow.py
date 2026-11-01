"""Play now for films: watch while it downloads (docs/design/video.md §9). The request goes to
Radarr as any other; once Radarr has handed a release to qBittorrent, the torrent is switched to
download in order, and the film's file is served as far as it has arrived, contiguously, through a
link signed for this account (a <video> can't send a token). When Radarr has imported it and
Jellyfin has it, the app moves over to Jellyfin's copy."""
import asyncio
import hashlib
import hmac
import os
import time

import httpx

from .. import config
from . import index
from .upstream import qbittorrent, radarr

LINK_SECONDS = 12 * 3600
# Ready to start: this much of the start in hand (and, for an MP4, its end: the index lives there).
READY_MIN_BYTES = 48 * 1024 * 1024
READY_FRACTION = 0.02
TAIL_BYTES = 8 * 1024 * 1024
# How long a jump past what's arrived waits for it (sequential mode fetches in order) before giving up.
WAIT_SECONDS = 25
# The most served in one response: the player asks again for what follows.
CHUNK_BYTES = 16 * 1024 * 1024
MIME = {".mkv": "video/x-matroska", ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
        ".webm": "video/webm", ".avi": "video/x-msvideo", ".ts": "video/mp2t"}


def signature(request_id: int, user_id: int, expires: int) -> str:
    msg = f"play-now-video|{request_id}|{user_id}|{expires}".encode()
    return hmac.new(config.settings.jwt_secret.encode(), msg, hashlib.sha256).hexdigest()


def verify(request_id: int, user_id: int, expires: int, sig: str) -> bool:
    return expires >= int(time.time()) and hmac.compare_digest(signature(request_id, user_id, expires), sig)


class Layout:
    """Where the film's file sits in its torrent: byte offset, size, piece size, path on disk."""

    def __init__(self, *, offset: int, size: int, piece_size: int, path: str, name: str):
        self.offset, self.size, self.piece_size, self.path, self.name = offset, size, piece_size, path, name

    def piece(self, byte: int) -> int:
        return (self.offset + byte) // self.piece_size

    def contiguous_from(self, states: list[int], start: int) -> int:
        """The end (exclusive) of the run of arrived bytes from [start]; [start] itself when it hasn't."""
        p = self.piece(start)
        last = self.piece(self.size - 1)
        while p <= last and p < len(states) and states[p] == 2:
            p += 1
        return max(start, min(self.size, p * self.piece_size - self.offset))

    def tail_in(self, states: list[int]) -> bool:
        return self.contiguous_from(states, max(0, self.size - TAIL_BYTES)) >= self.size

    @property
    def mime(self) -> str:
        return MIME.get(os.path.splitext(self.name)[1].lower(), "application/octet-stream")


def pick_file(torrent: dict, files: list[dict]) -> Layout | None:
    """The film in a torrent: its biggest video file (samples and extras are smaller)."""
    videos = [f for f in files if f.get("name", "").lower().endswith(qbittorrent.VIDEO_EXTENSIONS)]
    if not videos or not torrent.get("piece_size"):
        return None
    f = max(videos, key=lambda x: x.get("size") or 0)
    by_index = sorted(files, key=lambda x: x.get("index", 0))
    offset = sum(x.get("size") or 0 for x in by_index if x.get("index", 0) < f.get("index", 0))
    return Layout(offset=offset, size=f["size"], piece_size=torrent["piece_size"], path=local_path(torrent, f["name"]), name=f["name"])


def local_path(torrent: dict, name: str) -> str:
    """The file on continuo's mount: qBittorrent's download (or save) path, re-rooted, and the
    torrent's relative file name. Never outside the downloads folder."""
    s = config.settings
    root = os.path.realpath(s.video_downloads_dir)
    for base in (torrent.get("download_path"), torrent.get("save_path")):
        if not base:
            continue
        rel = os.path.relpath(base, s.qbittorrent_downloads_dir)
        if rel.startswith(".."):
            continue
        p = os.path.realpath(os.path.join(root, rel, name))
        if (p == root or p.startswith(root + os.sep)) and os.path.exists(p):
            return p
    return ""


async def find_download(req: dict) -> str | None:
    """The torrent Radarr handed over for this request (its hash), once it has."""
    if not req.get("radarr_movie_id"):
        return None
    for q in await radarr.queue_for(req["radarr_movie_id"]):
        if q.get("downloadId") and (q.get("protocol") in (None, "torrent")):
            return q["downloadId"].lower()
    return None


async def status(req: dict) -> dict:
    """Where Play now stands: searching, downloading (how much is in hand), ready to play, in the
    library (with its Jellyfin ID), or failed."""
    item = index.by_tmdb("movie", req["tmdb_id"])
    if item:
        return {"state": "available", "jellyfinId": item["jellyfin_id"]}
    if req["status"] == "failed":
        return {"state": "failed", "error": req.get("error")}
    if not qbittorrent.configured():
        return {"state": "unsupported", "error": "This server can't play films while they download (no qBittorrent set up)."}
    try:
        hash_ = await find_download(req)
        if not hash_:
            return {"state": "searching"}
        t = await qbittorrent.torrent(hash_)
        if not t:
            return {"state": "searching"}
        await qbittorrent.stream_mode(hash_, t)
        layout = pick_file(t, await qbittorrent.files(hash_))
        if not layout:
            return {"state": "downloading", "progress": t.get("progress") or 0, "note": "Waiting for the file list."}
        states = await qbittorrent.piece_states(hash_)
    except httpx.HTTPError as e:
        return {"state": "searching", "note": f"Checking the download: {e.__class__.__name__}"}
    have = layout.contiguous_from(states, 0)
    need = min(layout.size, max(READY_MIN_BYTES, int(layout.size * READY_FRACTION)))
    tail_ok = layout.mime != "video/mp4" or layout.tail_in(states)
    ready = have >= need and tail_ok and bool(layout.path)
    return {"state": "ready" if ready else "downloading", "progress": t.get("progress") or 0,
            "contiguousBytes": have, "neededBytes": need, "sizeBytes": layout.size,
            "downloadSpeed": t.get("dlspeed") or 0, "seeds": t.get("num_seeds") or 0, "etaSeconds": t.get("eta"),
            "container": os.path.splitext(layout.name)[1].lstrip(".").lower(), "mime": layout.mime,
            "onDisk": bool(layout.path)}


async def available(req: dict, start: int) -> tuple[Layout, int]:
    """The film's layout and how far from [start] can be served now, waiting a while for a jump
    ahead. Raises LookupError when there's no file yet, TimeoutError when it doesn't arrive."""
    hash_ = await find_download(req)
    t = await qbittorrent.torrent(hash_) if hash_ else None
    layout = pick_file(t, await qbittorrent.files(hash_)) if t else None
    if not layout or not layout.path:
        raise LookupError("No file to play yet.")
    if start >= layout.size:
        return layout, start
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        end = layout.contiguous_from(await qbittorrent.piece_states(hash_), start)
        if end > start:
            return layout, end
        if time.monotonic() >= deadline:
            raise TimeoutError("That part hasn't downloaded yet.")
        await asyncio.sleep(1)

