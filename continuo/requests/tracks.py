"""Single-song requests (#24): one song from Soulseek, filed under its album.

Lidarr only deals in whole albums, so a track request never touches it. The track loop takes
each request through: searching (slskd, the best copy by Soularr's format order; nobody has it
→ again later, on a backoff, "still looking") → downloading (following the slskd transfer; a
copy that fails or stalls is skipped for the next) → scanning (the file moved to
<SINGLES_FOLDER>/<Album Artist>/<Album>/ in the store, under the library's naming, tagged from
MusicBrainz; Navidrome scans) → available once Navidrome shows the song, in the requester's
library (#27). The album then reads "1 of N" (library/singles).

Lidarr never sees single songs: the singles folder isn't an artist folder of its root, so an
album is always whole or absent to Lidarr, and requesting it later is an ordinary whole-album
download; when it arrives, its singles go (library/singles.absorb)."""
import logging
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import config
from ..db import identity
from ..db import library as lib_db
from ..db import requests as req_db
from ..db.conn import now, parse_ts, seconds_since
from ..fetch import soulseek
from ..library import naming, ownership, singles, tags
from ..library.cache import Unavailable, music_dir
from ..upstream import navidrome
from . import matching, normalise
from .pipeline import build

log = logging.getLogger("continuo")

# Seconds before the next search after the nth fruitless one, then the last, forever.
BACKOFF = (120, 600, 1800, 3600, 3 * 3600)
STILL_LOOKING_AFTER = 2
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe(name: Optional[str], fallback: str) -> str:
    out = _UNSAFE.sub("_", (name or "").strip()).strip(" .")
    return out[:120] or fallback


async def prepare(recording_id: str, album_rg: str) -> dict:
    """Everything a track request needs from MusicBrainz: LookupError when it doesn't know the
    song, musicbrainz.Unavailable when it couldn't be asked."""
    from ..upstream import musicbrainz
    rec = await musicbrainz.recording(recording_id)
    if not rec or not rec.get("title"):
        raise LookupError("MusicBrainz doesn't know that song.")
    album = await musicbrainz.album_info(album_rg)
    if not album or not album.get("title"):
        raise LookupError("MusicBrainz doesn't know that song's album.")
    tracks = album.get("tracks") or []
    pos = next((t for t in tracks if t.get("musicBrainzRecordingId") == recording_id), None) \
        or next((t for t in tracks if normalise.normalise(t["title"]) == normalise.normalise(rec["title"])), None)
    # The library's name for the album artist (Ichiko Aoba, not 青葉市子), so the song files
    # under the folder its other albums are in.
    album_artist = identity.library_name(album.get("artistId")) if album.get("artistId") else None
    sort = None
    if not album_artist:
        pref = await naming.preferred(album.get("artistId"))
        album_artist, sort = (pref["name"], pref.get("sort")) if pref else (album.get("artist"), None)
    artist = rec.get("artist")
    if artist == album.get("artist"):
        artist = album_artist
    return {
        "title": rec["title"], "artist": artist, "artistId": rec.get("artistId"),
        "durationMs": rec.get("durationMs") or (pos or {}).get("durationMs"),
        "albumTitle": album["title"], "albumArtist": album_artist, "albumArtistSort": sort,
        "albumArtistId": album.get("artistId"), "date": album.get("date"), "year": album.get("year"),
        "releaseId": album.get("releaseId"), "imageUrl": album.get("imageUrl"),
        "position": (pos or {}).get("position"), "disc": (pos or {}).get("discNumber") or 1,
        "discs": max((t.get("discNumber") or 1 for t in tracks), default=1),
        "trackCount": len(tracks) or None,
    }


def album_folder(row: dict) -> str:
    """Where the song goes, relative to the store: <SINGLES_FOLDER>/<Album Artist>/<Album>."""
    t = row["track"]
    return (f"{singles.root()}/{_safe(t.get('albumArtist'), 'Unknown Artist')}"
            f"/{_safe(t.get('albumTitle'), 'Unknown Album')}")


def file_name(t: dict, ext: str) -> str:
    """The library's naming: "01 Title.flac" ("2-01 Title.flac" on a multi-disc album)."""
    pos = t.get("position")
    num = f"{int(pos):02d}" if pos else "00"
    if (t.get("discs") or 1) > 1:
        num = f"{t.get('disc') or 1}-{num}"
    return f"{num} {_safe(t.get('title'), 'Track')}.{ext}"


def _due(row: dict) -> bool:
    at = parse_ts(row.get("next_attempt_at"))
    return at is None or at <= datetime.now(timezone.utc)


def _later(cycles: int) -> str:
    wait = BACKOFF[min(cycles, len(BACKOFF)) - 1] if cycles else 0
    return (datetime.now(timezone.utc) + timedelta(seconds=wait)).isoformat(timespec="seconds")


def _broadcast(row: dict) -> None:
    from . import orchestrate
    orchestrate.broadcast(row)


def _status(row: dict, to: str, **fields) -> dict:
    if row["status"] != to:
        req_db.add_event(row["id"], "status", {"from": row["status"], "to": to})
    row = req_db.update(row["id"], status=to, **fields)
    _broadcast(row)
    return row


async def _search(row: dict) -> dict:
    t = row["track"]
    fetch = row.get("fetch") or {}
    tried = {tuple(x) for x in fetch.get("tried") or []}
    refused = set(fetch.get("refused") or [])
    try:
        responses = await soulseek.search_any(soulseek.queries(t.get("artist") or "", t["title"]))
    except soulseek.Offline:
        # Not a search that found nothing: it doesn't count, and it's tried again soon.
        log.info("track %s: Soulseek is offline, trying again later", row["id"])
        return _status(row, "searching", next_attempt_at=_later(1))
    # Copies that already failed don't count, nor anything from someone who rejected us.
    responses = [r for r in responses if r.get("username") not in refused]
    for r in responses:
        r["files"] = [f for f in r.get("files") or [] if (r.get("username"), f.get("filename")) not in tried]
    best = soulseek.best(responses, t.get("artist") or "", t["title"], t.get("durationMs"), variants=False)
    req_db.update(row["id"], last_search_at=now())
    if best is None:
        cycles = (row.get("search_cycles") or 0) + 1
        req_db.add_event(row["id"], "track_search", {"found": False, "responses": len(responses)})
        return _status(row, "searching", search_cycles=cycles, still_looking=cycles >= STILL_LOOKING_AFTER,
                       next_attempt_at=_later(cycles))
    try:
        await soulseek.enqueue(best)
    except Exception as e:  # noqa: BLE001 — slskd refused: next time, maybe another copy
        log.warning("track %s: enqueue failed: %s", row["id"], e)
        return _status(row, "searching", next_attempt_at=_later(1))
    req_db.add_event(row["id"], "track_download", {"user": best.username, "file": best.basename,
                                                   "format": best.extension, "bitRate": best.bit_rate})
    fetch = {"username": best.username, "filename": best.filename, "size": best.size,
             "extension": best.extension, "startedAt": now(), "percent": 0,
             "tried": sorted(tried), "refused": sorted(refused)}
    return _status(row, "downloading", fetch=fetch, last_source="soulseek", still_looking=False,
                   download_started_at=row.get("download_started_at") or now())


def _candidate(fetch: dict) -> soulseek.Candidate:
    return soulseek.Candidate(username=fetch["username"], filename=fetch["filename"], size=fetch.get("size") or 0,
                              extension=fetch.get("extension") or "", bit_rate=None, length_s=None,
                              free_slot=False, queue_length=0, upload_speed=0)


def _downloaded_path(c: soulseek.Candidate) -> Optional[str]:
    """Where slskd saved the copy: <downloads>/<remote parent folder>/<file> (slskd adds a
    _<ticks> suffix when the name was taken)."""
    base = config.settings.slskd_downloads_dir
    if not base:
        return None
    folder = os.path.join(base, c.folder)
    exact = os.path.join(folder, c.basename)
    if os.path.isfile(exact):
        return exact
    stem, ext = os.path.splitext(c.basename)
    try:
        hits = sorted(f for f in os.listdir(folder) if f.startswith(stem + "_") and f.endswith(ext))
    except OSError:
        return None
    return os.path.join(folder, hits[-1]) if hits else None


async def cancel(row: dict) -> None:
    """Stop a song's download in slskd (the request is going away). Best effort."""
    from ..upstream import slskd
    fetch = row.get("fetch") or {}
    if not fetch.get("username"):
        return
    try:
        t = await soulseek.transfer(_candidate(fetch))
        if t and t.get("id") and soulseek.finished(t) is None:
            await slskd.cancel(fetch["username"], t["id"])
    except Exception as e:  # noqa: BLE001
        log.warning("track %s: cancel failed: %s", row["id"], e)


async def _next_copy(row: dict, why: str) -> dict:
    fetch = row.get("fetch") or {}
    tried = [list(x) for x in fetch.get("tried") or []] + [[fetch.get("username"), fetch.get("filename")]]
    refused = set(fetch.get("refused") or [])
    if "Rejected" in why and fetch.get("username"):
        refused.add(fetch["username"])   # they'll reject their other copies too (friends-only shares)
    req_db.add_event(row["id"], "track_copy_failed", {"user": fetch.get("username"), "why": why})
    await cancel(row)
    return _status(row, "searching", fetch={"tried": tried, "refused": sorted(refused)}, next_attempt_at=now())


async def _follow(row: dict) -> dict:
    fetch = row.get("fetch") or {}
    c = _candidate(fetch)
    t = await soulseek.transfer(c)
    done = soulseek.finished(t)
    if t is None or done is False:
        return await _next_copy(row, "gone" if t is None else (t.get("state") or "failed"))
    if done is None:
        pct = round(float(t.get("percentComplete") or 0), 1)
        if (seconds_since(fetch.get("startedAt")) or 0) > config.settings.track_download_give_up_seconds:
            return await _next_copy(row, "too slow")
        if pct != fetch.get("percent"):
            row = req_db.update(row["id"], fetch={**fetch, "percent": pct})
            _broadcast(row)
        return row
    path = _downloaded_path(c)
    if path is None:
        return await _next_copy(row, "downloaded file not found")
    return await _file(row, path)


async def _file(row: dict, path: str) -> dict:
    """Into the store: the album's folder, the library's naming, MusicBrainz tags."""
    t = row["track"]
    try:
        store = music_dir()
    except Unavailable as e:
        await _attention(row, "not_filed", f"The song downloaded but couldn't be filed: {e}")
        return row
    ext = os.path.splitext(path)[1].lstrip(".").lower() or (row.get("fetch") or {}).get("extension") or "flac"
    folder = album_folder(row)
    rel = f"{folder}/{file_name(t, ext)}"
    dest = os.path.realpath(os.path.join(store, rel))
    if not dest.startswith(store + os.sep):
        await _attention(row, "not_filed", "The song's album folder isn't inside the library.")
        return row
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        os.remove(path)          # already there (requested before): keep that copy
    else:
        tags.write_track_tags(path, {
            "title": t.get("title"), "artist": t.get("artist"), "album": t.get("albumTitle"),
            "albumartist": t.get("albumArtist"), "albumartistsort": t.get("albumArtistSort"),
            "tracknumber": t.get("position"), "discnumber": t.get("disc"), "date": t.get("date"),
            "musicbrainz_trackid": row["mb_release_group_id"], "musicbrainz_albumid": t.get("releaseId"),
            "musicbrainz_releasegroupid": row.get("album_release_group_id"),
            "musicbrainz_artistid": t.get("artistId"), "musicbrainz_albumartistid": t.get("albumArtistId"),
        })
        shutil.move(path, dest)
    log.info("track %s: %r filed at %s", row["id"], t.get("title"), rel)
    req_db.add_event(row["id"], "track_filed", {"path": rel})
    row = _status(row, "scanning", file_path=rel, first_file_at=row.get("first_file_at") or now(),
                  first_source=row.get("first_source") or "soulseek", scan_started_at=now())
    try:
        await navidrome.start_scan()
    except Exception as e:  # noqa: BLE001 — the watcher picks it up anyway
        log.warning("track %s: scan didn't start: %s", row["id"], e)
    return row


async def _file_local(row: dict, local: str) -> dict:
    """A kept Play now song (#12): its cached copy is filed like a download, the cache keeping its
    own until evicted. When the copy's gone (evicted meanwhile) it's searched for like any song."""
    if not os.path.isfile(local):
        req_db.add_event(row["id"], "keep_copy_gone", {"path": local})
        return req_db.update(row["id"], fetch={})
    tmp = f"{local}.keep"
    shutil.copy2(local, tmp)
    req_db.update(row["id"], fetch={"extension": (row.get("fetch") or {}).get("extension")},
                  last_source="soulseek", download_started_at=row.get("download_started_at") or now())
    return await _file(req_db.get(row["id"]), tmp)


async def _attention(row: dict, reason: str, detail: str) -> None:
    from ..workers import scan
    await scan.attention(row, reason, detail)


async def _visible(row: dict) -> dict:
    """Available once Navidrome shows the song: in the requester's own library when it has one."""
    from ..workers import index, scan
    rel = row["file_path"]
    folder = rel.rsplit("/", 1)[0]
    await index.refresh()
    for album in lib_db.by_folder(folder):
        paths = await navidrome.album_song_paths(album["navidrome_id"], limit=1000)
        if rel in {navidrome.relative_path(p) for p in paths}:
            ready, mine = await ownership.claim(row.get("user_id"), album)
            if not ready:
                return row
            return await scan.mark_available(row, matching.Match(strategy="track_file",
                                                                 navidrome_album_id=mine or album["navidrome_id"]))
    if (seconds_since(row.get("scan_started_at")) or 0) > config.settings.scan_timeout_seconds:
        await _attention(row, "import_not_visible", "Filed in the library but not visible in Navidrome yet.")
    return row


async def _prepare_later(row: dict) -> dict:
    """A song requested while MusicBrainz was too busy to look up: its details now, or later."""
    from ..upstream import musicbrainz
    try:
        t = await prepare(row["mb_release_group_id"], row["album_release_group_id"])
    except musicbrainz.Unavailable as e:
        log.info("track %s: MusicBrainz still busy (%s); trying again later", row["id"], e)
        return req_db.update(row["id"], next_attempt_at=_later(1))
    except LookupError as e:
        req_db.add_event(row["id"], "status", {"from": row["status"], "to": "error"})
        row = req_db.update(row["id"], status="error", error=str(e))
        _broadcast(row)
        return row
    row = req_db.update(row["id"], track=t, title=t["title"], artist=t["artist"], year=t.get("year"),
                        artwork_url=t.get("imageUrl"), next_attempt_at=None)
    req_db.add_event(row["id"], "track_details", {"album": t["albumTitle"]})
    _broadcast(row)
    return row


async def step(row: dict) -> dict:
    """One move forward for one track request."""
    status = row["status"]
    if not row.get("track"):
        return await _prepare_later(row) if _due(row) else row
    local = (row.get("fetch") or {}).get("local")
    if status in ("pending", "searching") and local:
        return await _file_local(row, local)
    if status in ("pending", "searching") and _due(row):
        return await _search(row)
    if status == "downloading":
        return await _follow(row)
    if status == "scanning" and row.get("file_path"):
        return await _visible(row)
    return row


async def tick() -> None:
    for row in req_db.list_requests(statuses=("pending", "searching", "downloading", "scanning"), kind="track"):
        try:
            await step(row)
        except Exception:  # noqa: BLE001 — one bad request never stops the others
            log.exception("track request %s failed a step", row["id"])


def search_now(row: dict) -> str:
    fields = {"next_attempt_at": now(), "search_cycles": 0, "still_looking": False, "attention": None, "error": None}
    if row["status"] in ("error", "needs_attention"):
        fields["status"] = "searching"
    req_db.update(row["id"], **fields)
    req_db.add_event(row["id"], "search_fired", {"by": "search_now"})
    return "Searching Soulseek now — this can take a minute."


def pipeline(row: dict) -> dict:
    """The Requests screen's view of a track request."""
    status = row.get("status")
    fetch = row.get("fetch") or {}
    if status == "available":
        return build("available", percent=100, detail="In your library — ready to play.", row=row)
    if status == "needs_attention":
        att = row.get("attention") or {}
        return build("scanning", percent=100, detail=att.get("detail") or "Needs a look.", attention=att, row=row)
    if status == "error":
        return build("requested", detail=row.get("error") or "Couldn't request this song.", row=row)
    if status == "scanning":
        return build("scanning", percent=100, files=1, total=1, source="soulseek",
                     detail="Downloaded — waiting for Navidrome to pick it up.", row=row)
    if status == "downloading":
        return build("downloading", percent=fetch.get("percent") or 0, files=0, total=1, source="soulseek",
                     detail=f"Downloading from Soulseek ({fetch.get('extension') or 'audio'}) from {fetch.get('username')}",
                     download={"client": "slskd", "sizeLeft": None, "timeLeft": None, "stalled": False,
                               "filesDone": 0, "filesTotal": 1},
                     row=row)
    if not row.get("track"):
        return build("requested", total=1, row=row,
                     detail="Waiting for MusicBrainz (busy right now) to look the song up; it'll start by itself.")
    if row.get("still_looking"):
        return build("searching", total=1, row=row,
                     detail=f"Still looking — nobody on Soulseek has it yet ({row.get('search_cycles') or 0} searches). "
                            "Searching continues automatically; you can request the album instead.")
    return build("searching" if status == "searching" else "requested", total=1, row=row,
                 detail="Searching Soulseek for this song…")
