"""Play now for films and episodes: watch while it downloads (docs/design/video.md §9). The request goes to
Radarr as any other; once Radarr has handed a release to qBittorrent, the torrent is switched to
download in order, and the film's file is served as far as it has arrived, contiguously, through a
link signed for this account (a <video> can't send a token). When Radarr has imported it and
Jellyfin has it, the app moves over to Jellyfin's copy."""
import asyncio
import hashlib
import hmac
import logging
import os
import re
import time

import httpx

from .. import config
from . import index
from .upstream import qbittorrent, radarr, sonarr

log = logging.getLogger("continuo")
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


# An episode: (season, number). A film: None.
Ep = tuple[int, int] | None


def signature(request_id: int, user_id: int, expires: int, ep: Ep = None) -> str:
    s, e = ep or (0, 0)
    msg = f"play-now-video|{request_id}|{user_id}|{expires}|{s}|{e}".encode()
    return hmac.new(config.settings.jwt_secret.encode(), msg, hashlib.sha256).hexdigest()


def verify(request_id: int, user_id: int, expires: int, sig: str, ep: Ep = None) -> bool:
    return expires >= int(time.time()) and hmac.compare_digest(signature(request_id, user_id, expires, ep), sig)


_EPISODE_NAMES = (re.compile(r"[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})(?!\d)"), re.compile(r"(?<!\d)(\d{1,2})x(\d{2,3})(?!\d)"))


def episode_in_name(name: str) -> tuple[int, int] | None:
    """S01E02, s1e2, 1x02: which episode a file is, by its name (the folder's too)."""
    for pattern in _EPISODE_NAMES:
        found = pattern.findall(os.path.basename(name)) or pattern.findall(name)
        if found:
            s, e = found[-1]
            return int(s), int(e)
    return None


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


def pick_file(torrent: dict, files: list[dict], ep: Ep = None) -> Layout | None:
    """The film in a torrent: its biggest video file (samples and extras are smaller). An episode:
    the file named for it (a season pack holds many), or the only video when there's one."""
    videos = [f for f in files if f.get("name", "").lower().endswith(qbittorrent.VIDEO_EXTENSIONS)]
    if not videos or not torrent.get("piece_size"):
        return None
    if ep is not None:
        named = [f for f in videos if episode_in_name(f["name"]) == ep]
        biggest = max(videos, key=lambda x: x.get("size") or 0)
        lone = len([f for f in videos if (f.get("size") or 0) > (biggest.get("size") or 0) * 0.2]) == 1
        videos = named or ([biggest] if lone else [])
        if not videos:
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


async def find_download(req: dict, ep: Ep = None) -> str | None:
    """The torrent Radarr (a film) or Sonarr (the episode, alone or in a season pack) handed over for
    this request (its hash), once it has."""
    if ep is None:
        if not req.get("radarr_movie_id"):
            return None
        queue = await radarr.queue_for(req["radarr_movie_id"])
    else:
        if not req.get("sonarr_series_id"):
            return None
        queue = [q for q in await sonarr.queue_for(req["sonarr_series_id"])
                 if ((q.get("episode") or {}).get("seasonNumber"), (q.get("episode") or {}).get("episodeNumber")) == ep]
    for q in queue:
        if q.get("downloadId") and (q.get("protocol") in (None, "torrent")):
            return q["downloadId"].lower()
    return None


async def in_library(req: dict, ep: Ep = None) -> str | None:
    """Jellyfin's ID for the film, or the episode, once the library has it."""
    if ep is None:
        item = index.by_tmdb("movie", req["tmdb_id"])
        return item["jellyfin_id"] if item else None
    show = index.by_tmdb("series", req["tmdb_id"])
    if not show:
        return None
    from .upstream import jellyfin
    try:
        for e in await jellyfin.episodes(show["jellyfin_id"], season=ep[0]):
            if (e["season"], e["episode"]) == ep:
                return e["jellyfinId"]
    except httpx.HTTPError:
        pass
    return None


async def status(req: dict, ep: Ep = None) -> dict:
    """Where Play now stands: searching, downloading (how much is in hand), ready to play, in the
    library (with its Jellyfin ID), or failed."""
    jellyfin_id = await in_library(req, ep)
    if jellyfin_id:
        return {"state": "available", "jellyfinId": jellyfin_id}
    if req["status"] == "failed":
        return {"state": "failed", "error": req.get("error")}
    if not qbittorrent.configured():
        return {"state": "unsupported", "error": "This server can't play films while they download (no qBittorrent set up)."}
    try:
        hash_ = await find_download(req, ep)
        if not hash_:
            return {"state": "searching"}
        t = await qbittorrent.torrent(hash_)
        if not t:
            return {"state": "searching"}
        await qbittorrent.stream_mode(hash_, t)
        layout = pick_file(t, await qbittorrent.files(hash_), ep)
        if not layout:
            return {"state": "downloading", "progress": t.get("progress") or 0, "note": "Waiting for the file list."}
        states = await qbittorrent.piece_states(hash_)
    except httpx.HTTPError as e:
        return {"state": "searching", "note": f"Checking the download: {e.__class__.__name__}"}
    have = layout.contiguous_from(states, 0)
    need = min(layout.size, max(READY_MIN_BYTES, int(layout.size * READY_FRACTION)))
    tail_ok = layout.mime != "video/mp4" or layout.tail_in(states)
    ready = have >= need and tail_ok and bool(layout.path)
    # Slower than it plays (a two-hour film assumed), a minute in: worth a faster copy.
    playback_rate = layout.size / (2 * 3600)
    too_slow = (t.get("time_active") or 0) > 60 and (t.get("progress") or 0) < 0.95 and (t.get("dlspeed") or 0) < playback_rate * 1.1
    # What indexers listed isn't what's there: a film a minute and a half in with few real sources, or
    # too slow, is swapped for the next best (once; a series' pack is left to the button).
    if ep is None and req["id"] not in _swapped and (t.get("time_active") or 0) > VERIFY_AFTER_SECONDS and (t.get("progress") or 0) < 0.5 \
            and ((t.get("num_seeds") or 0) < MIN_REAL_SOURCES or too_slow):
        _swapped.add(req["id"])
        try:
            if await find_faster(req):
                return {"state": "searching", "note": "That copy had too few sources: switched to a better-shared one."}
        except httpx.HTTPError as e:
            log.warning("play now %s: swapping a thin copy failed: %s", req["id"], e)
    return {"state": "ready" if ready else "downloading", "progress": t.get("progress") or 0, "tooSlow": too_slow,
            "contiguousBytes": have, "neededBytes": need, "sizeBytes": layout.size,
            "downloadSpeed": t.get("dlspeed") or 0, "seeds": t.get("num_seeds") or 0, "etaSeconds": t.get("eta"),
            "container": os.path.splitext(layout.name)[1].lstrip(".").lower(), "mime": layout.mime,
            "onDisk": bool(layout.path)}


_swapped: set[int] = set()
_mode_checked: dict[str, float] = {}
MODE_RECHECK_SECONDS = 30

# Where a request's file is, kept a minute: the player asks for the next 16 MB every few seconds, and
# looking it up each time (Radarr's or Sonarr's queue, the torrent, its files) put a pause between them.
_layouts: dict[tuple, tuple[float, str, Layout]] = {}
LAYOUT_SECONDS = 60


async def _layout(req: dict, ep: Ep) -> tuple[str, Layout] | None:
    key = (req["id"], ep)
    hit = _layouts.get(key)
    if hit and hit[0] > time.monotonic() and os.path.exists(hit[2].path):
        return hit[1], hit[2]
    hash_ = await find_download(req, ep)
    t = await qbittorrent.torrent(hash_) if hash_ else None
    layout = pick_file(t, await qbittorrent.files(hash_), ep) if t else None
    if not layout or not layout.path:
        _layouts.pop(key, None)
        return None
    _layouts[key] = (time.monotonic() + LAYOUT_SECONDS, hash_, layout)
    return hash_, layout


async def available(req: dict, start: int, ep: Ep = None) -> tuple[Layout, int]:
    """The file's layout and how far from [start] can be served now, waiting a while for a jump
    ahead. Raises LookupError when there's no file yet, TimeoutError when it doesn't arrive."""
    found = await _layout(req, ep)
    if not found:
        raise LookupError("No file to play yet.")
    hash_, layout = found
    # While it's being watched, keep it downloading in order (it has been found switched back off).
    if time.monotonic() - _mode_checked.get(hash_, 0) > MODE_RECHECK_SECONDS:
        _mode_checked[hash_] = time.monotonic()
        t = await qbittorrent.torrent(hash_)
        if t:
            await qbittorrent.stream_mode(hash_, t)
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



# --- the release to stream: Play now picks it, rather than Radarr's best-quality choice ------------

# Radarr's own objections that don't matter for Play now (one already in the queue, say).
_SOFT_REJECTIONS = ("already meets cutoff", "in queue", "is not an upgrade", "existing file")
_HDR = re.compile(r"\b(dv|dovi|dolby[ ._-]?vision|hdr10\+?|hdr)\b", re.I)
_H264 = re.compile(r"\b(x264|h[ ._]?264|avc)\b", re.I)
_HEVC = re.compile(r"\b(x265|h[ ._]?265|hevc)\b", re.I)
# Indexers' seeder counts run high (Interstellar's SURGE: 10 listed, 2 in the swarm): ask for plenty,
# settle for fewer only when nothing has them, and check the real swarm once it's downloading.
MIN_SEEDERS = 20
FALLBACK_SEEDERS = 5
# Radarr's search answered within seconds with one indexer's results; a second look gets the rest.
RESEARCH_SECONDS = 20
# A minute and a half in, fewer real sources than this, or slower than it plays: swap it (once).
VERIFY_AFTER_SECONDS = 90
MIN_REAL_SOURCES = 5
MAX_MBPS = 12.0


def streamable(release: dict, runtime_minutes: int | None, min_seeders: int = MIN_SEEDERS) -> float | None:
    """How good a release is to watch while it downloads, or None when it isn't one: a torrent at
    1080p (720p will do), not 4K, a remux or HDR (for SDR screens and browsers), with enough
    sources, at a bitrate a home connection keeps up with. Most sources wins; H.264 (any browser
    plays it) and 3-8 Mbit/s are preferred."""
    if release.get("protocol") != "torrent":
        return None
    title = release.get("title") or ""
    rejections = [x for x in release.get("rejections") or [] if not any(s in x.lower() for s in _SOFT_REJECTIONS)]
    if rejections:
        return None
    quality = ((release.get("quality") or {}).get("quality") or {})
    resolution = quality.get("resolution") or 0
    if resolution not in (720, 1080) or "remux" in (quality.get("name") or "").lower() or _HDR.search(title):
        return None
    seeders = release.get("seeders") or 0
    if seeders < min_seeders:
        return None
    mbps = (release.get("size") or 0) * 8 / ((runtime_minutes or 120) * 60) / 1e6
    if not 0.8 <= mbps <= MAX_MBPS:
        return None
    score = min(seeders, 150) ** 0.5               # past ~150 sources a home connection is full anyway
    score += 6 if resolution == 1080 else 0
    score += 4 if _H264.search(title) else 1 if _HEVC.search(title) else 0
    score += 3 if 2.5 <= mbps <= 8 else 0
    return score


def best_release(found: list[dict], runtime_minutes: int | None, min_seeders: int = MIN_SEEDERS,
                 exclude: set[str] = frozenset()) -> dict | None:
    scored = [(streamable(r, runtime_minutes, min_seeders), r) for r in found if r.get("title") not in exclude]
    scored = [(s, r) for s, r in scored if s is not None]
    return max(scored, key=lambda x: x[0])[1] if scored else None


async def pick_and_grab(movie: dict) -> dict | None:
    """Grabs the best release to stream for a film in Radarr; None when there isn't one. Without a
    well-shared one at first, it looks again (the first answer can be one indexer's), then settles."""
    runtime = movie.get("runtime")
    chosen = best_release(await radarr.releases(movie["id"]), runtime)
    if not chosen:
        await asyncio.sleep(RESEARCH_SECONDS)
        found = await radarr.releases(movie["id"])
        chosen = best_release(found, runtime) or best_release(found, runtime, FALLBACK_SEEDERS)
    if chosen:
        log.info("play now: grabbing %s (%s sources listed)", chosen.get("title"), chosen.get("seeders"))
        await radarr.grab(chosen)
    return chosen


async def find_faster(req: dict) -> dict | None:
    """The download is too slow to watch: drop it (blocklisted) for the best release to stream."""
    movie_id = req.get("radarr_movie_id")
    if not movie_id:
        return None
    movie = (await radarr._client().get(f"/movie/{movie_id}")).json()
    queue = await radarr.queue_for(movie_id)
    current = {q.get("title") for q in queue}
    found = await radarr.releases(movie_id)
    chosen = best_release(found, movie.get("runtime"), exclude=current) or \
        best_release(found, movie.get("runtime"), FALLBACK_SEEDERS, exclude=current)
    if not chosen:
        return None
    for q in queue:
        await radarr.drop_download(q["id"])
    log.info("play now: swapping %s for %s (%s sources listed)", ", ".join(sorted(t for t in current if t)),
             chosen.get("title"), chosen.get("seeders"))
    _layouts.pop((req["id"], None), None)
    await radarr.grab(chosen)
    return chosen
