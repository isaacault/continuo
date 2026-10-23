# Portions derived from lidseeker (https://github.com/IvoryCobra-VC/lidseeker), MIT License.
"""Where a request sits: requested → searching → downloading → importing → scanning → available.

Computed live from Lidarr (album statistics, queue), slskd (Soulseek transfers) and Soularr's
denylist. Best-effort: an upstream hiccup degrades to a coarser stage, never an error. There is
no failed state: an album nobody has yet is "searching" with `stillLooking` as a display flag.
"""
import time
from typing import Optional

import httpx

from .. import config
from ..db.conn import seconds_since
from ..upstream import lidarr, slskd, soularr

STAGES = ["requested", "searching", "downloading", "importing", "scanning", "available"]

# stage → the request status the status loop stores
STAGE_STATUS = {
    "requested": "pending",
    "searching": "searching",
    "downloading": "downloading",
    "importing": "importing",
    "scanning": "scanning",
    "available": "available",
}

_IMPORT_STATES = ("importpending", "importing", "importblocked", "imported")

# queue id -> (sizeLeft, monotonic time it was first seen at that size)
_progress: dict[int, tuple[Optional[int], float]] = {}


def build(stage: str, *, percent: float = 0, files: int = 0, total: int = 0, detail: str = "",
          source: Optional[str] = None, download: Optional[dict] = None,
          attention: Optional[dict] = None, row: Optional[dict] = None) -> dict:
    row = row or {}
    return {
        "stages": STAGES,
        "stage": stage,
        "stageIndex": STAGES.index(stage) if stage in STAGES else 0,
        "percent": round(percent or 0, 1),
        "trackFiles": files,
        "trackCount": total,
        "source": source,
        "detail": detail,
        "lastSearchAt": row.get("last_search_at"),
        "searchCycles": row.get("search_cycles") or 0,
        "download": download,
        "attention": attention,
    }


def _ago(ts: Optional[str]) -> str:
    secs = seconds_since(ts)
    if secs is None:
        return ""
    if secs < 3600:
        return f"{max(1, int(secs // 60))}m ago"
    if secs < 86400 * 2:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _stalled(q: dict) -> bool:
    """No byte of progress for STALLED_DOWNLOAD_HOURS, or the client itself says stalled."""
    text = " ".join(q.get("messages") or []).lower() + " " + (q.get("errorMessage") or "").lower()
    if "stalled" in text:
        return True
    qid = q.get("id")
    if qid is None:
        return False
    now = time.monotonic()
    prev = _progress.get(qid)
    if prev is None or prev[0] != q.get("sizeLeft"):
        _progress[qid] = (q.get("sizeLeft"), now)
        return False
    return now - prev[1] > config.settings.stalled_download_hours * 3600


def forget_queue_items(present_ids: set[int]) -> None:
    for qid in list(_progress):
        if qid not in present_ids:
            del _progress[qid]


def _searching_detail(row: dict) -> str:
    cycles = row.get("search_cycles") or 0
    if row.get("still_looking"):
        last = _ago(row.get("last_search_at"))
        return (f"Still looking — {cycles} search cycles"
                + (f", last search {last}" if last else "")
                + ". Searching continues automatically.")
    return "Searching torrents and Soulseek for a source…"


async def compute(row: dict, queue_index: Optional[dict[int, dict]] = None) -> dict:
    status = row.get("status")
    if status == "error":
        return build("requested", detail=row.get("error") or "Couldn't add this to Lidarr.",
                     row=row)
    if status == "available":
        return build("available", percent=100, detail="In your library — ready to play.", row=row)
    if status == "needs_attention":
        att = row.get("attention") or {}
        stage = "scanning" if att.get("reason") in ("import_not_visible", "ambiguous_match") \
            else "importing"
        return build(stage, percent=100 if stage == "scanning" else 0,
                     detail=att.get("detail") or "Needs a look.", attention=att, row=row)

    album_id = row.get("lidarr_album_id")
    if not album_id:
        return build("requested", detail="Adding to Lidarr…", row=row)

    try:
        album = await lidarr.get_album(album_id)
    except httpx.HTTPError:
        album = None
    stats = (album or {}).get("statistics") or {}
    pct = stats.get("percentOfTracks") or 0
    files = stats.get("trackFileCount") or 0
    total = stats.get("trackCount") or 0

    if album is not None and (not album.get("releases") or not stats):
        return build("requested", detail="Waiting for Lidarr to load this album's details…",
                     row=row)
    if status == "scanning" or (total and files >= total) or pct >= 100:
        return build("scanning", percent=100, files=files, total=total,
                     detail="Downloaded — waiting for Navidrome to pick it up.", row=row)

    # Soulseek: Soularr downloads via slskd before handing the folder to Lidarr, a phase
    # Lidarr's own queue can't see.
    prog = await slskd.album_progress(row.get("artist"), row.get("title"))
    if prog and prog["active"]:
        # A race (#31): Soulseek started because the torrent was slow; both keep going.
        racing = ""
        tq = (queue_index or {}).get(album_id) if queue_index is not None else None
        if tq and tq.get("protocol") == "torrent" and tq.get("size"):
            t_pct = round(100 * (tq["size"] - (tq.get("sizeLeft") or 0)) / tq["size"])
            racing = f" · racing the torrent ({t_pct}%)"
        return build(
            "downloading", percent=prog["percent"], files=files, total=total, source="soulseek",
            detail=f"Downloading from Soulseek — {prog['filesDone']}/{prog['filesTotal']} files"
                   + (f" from {prog['username']}" if prog.get("username") else "") + racing,
            download={"client": "slskd", "sizeLeft": None, "timeLeft": None, "stalled": False,
                      "filesDone": prog["filesDone"], "filesTotal": prog["filesTotal"]},
            row=row,
        )

    if queue_index is None:
        queue_index = await lidarr.queue_index()
    q = queue_index.get(album_id)
    if q:
        state = (q.get("trackedDownloadState") or q.get("status") or "").lower()
        source = "torrent" if q.get("protocol") == "torrent" else (q.get("protocol") or None)
        if state in _IMPORT_STATES:
            detail = "Importing into your library…"
            if state == "importblocked" or q.get("trackedDownloadStatus") == "warning":
                msg = "; ".join(q.get("messages") or []) or "Lidarr couldn't import it automatically."
                detail = f"Import is blocked — {msg}"
            return build("importing", percent=pct, files=files, total=total, source=source,
                         detail=detail, row=row)
        stalled = _stalled(q)
        size, left = q.get("size") or 0, q.get("sizeLeft")
        dl_pct = round(100 * (size - left) / size, 1) if size and left is not None else pct
        if stalled:
            detail = ("Torrent stalled — torrent-cleanup replaces it after 24 h, then Soulseek "
                      "gets a turn. You can drop it now.")
        else:
            detail = f"Downloading via {q.get('downloadClient') or source or 'Lidarr'}"
            if q.get("timeLeft"):
                detail += f" — {q['timeLeft']} left"
        return build(
            "downloading", percent=dl_pct, files=files, total=total, source=source, detail=detail,
            download={"client": q.get("downloadClient"), "sizeLeft": left,
                      "timeLeft": q.get("timeLeft"), "stalled": stalled, "queueId": q.get("id")},
            row=row,
        )

    if 0 < files < total:
        return build("importing", percent=pct, files=files, total=total,
                     detail=f"Imported {files}/{total} tracks — finishing up.", row=row)

    if soularr.is_denylisted(album_id):
        return build("searching", percent=pct, files=files, total=total,
                     detail="A Soulseek download failed to import. Retry clears it and searches "
                            "again; searching otherwise continues.", row=row)

    return build("searching", percent=pct, files=files, total=total,
                 detail=_searching_detail(row), row=row)


def in_flight(p: dict) -> bool:
    """Real progress: anything past searching (a reason to reset the fruitless-cycle count)."""
    return p["stage"] in ("downloading", "importing", "scanning", "available")
