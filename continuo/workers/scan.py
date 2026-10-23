"""Scan watcher: from "Lidarr has the files" to "playable in Navidrome".

While any request is `scanning` (or needs_attention because its import isn't visible / the match
was ambiguous), every SCAN_POLL_SECONDS:
  * notice Navidrome scans (its own watcher or ours) by lastScan changing → refresh the index;
  * try to match each waiting request; on a match → available + notify;
  * if nothing appeared SCAN_WATCHER_GRACE_SECONDS after the files landed, ask for one scan
    (once per import burst);
  * after SCAN_TIMEOUT_SECONDS → needs_attention "imported but not visible in Navidrome" +
    notify; keep watching so a later scan still recovers it to available.
"""
import logging
import time

import httpx

from .. import config
from ..db import library as lib_db
from ..db import requests as req_db
from ..db.conn import now, seconds_since
from ..library import ownership
from ..notify import dispatch
from ..requests import matching, orchestrate
from ..upstream import lidarr, navidrome, slskd
from ..upstream.navidrome import NavidromeError
from . import index

log = logging.getLogger("continuo")

_WATCH_REASONS = ("import_not_visible", "ambiguous_match")

_state = {"last_scan_seen": None, "scan_requested_at": 0.0, "scanning": False}


def waiting() -> list[dict]:
    rows = req_db.list_requests(statuses=("scanning", "needs_attention"))
    return [r for r in rows if r["status"] == "scanning"
            or (r.get("attention") or {}).get("reason") in _WATCH_REASONS]


async def _paths_for(row: dict) -> list[str]:
    """Imported file paths: the webhook's, else ask Lidarr (so matching by path works without
    the webhook too)."""
    if row.get("import_paths"):
        return row["import_paths"]
    if not row.get("lidarr_album_id"):
        return []
    try:
        files = await lidarr.track_files(row["lidarr_album_id"])
    except httpx.HTTPError:
        return []
    paths = [f.get("path") for f in files if f.get("path")]
    if paths:
        req_db.update(row["id"], import_paths=paths)
    return paths


async def mark_available(row: dict, m: matching.Match) -> dict:
    ts = now()
    row = req_db.update(row["id"], status="available", navidrome_album_id=m.navidrome_album_id,
                        available_at=ts, attention=None, still_looking=False)
    req_db.add_event(row["id"], "matched", {"strategy": m.strategy,
                                            "navidromeAlbumId": m.navidrome_album_id, **m.detail})
    req_db.add_event(row["id"], "status", {"to": "available"})
    orchestrate.broadcast(row)
    from .. import events
    from ..requests.view import play
    events.publish("request.available", {"requestId": row["id"], "play": play(row)},
                   row.get("user_id"))
    await dispatch.request_event(row, "request_available")
    try:
        n = await slskd.delete_searches_for(row.get("artist"), row.get("title"))
        if n:
            log.info("cleaned up %d slskd search(es) for request %s", n, row["id"])
    except Exception:  # noqa: BLE001 — cleanup is best-effort
        log.exception("slskd cleanup failed for request %s", row["id"])
    return row


async def attention(row: dict, reason: str, detail: str, candidates: list | None = None,
                    notify: bool = True) -> dict:
    current = row.get("attention") or {}
    att = {"reason": reason, "detail": detail, "candidates": candidates or [], "since": now()}
    if row["status"] == "needs_attention" and current.get("reason") == reason:
        if current.get("candidates") != att["candidates"]:
            att["since"] = current.get("since", att["since"])
            row = req_db.update(row["id"], attention=att)
        return row
    row = req_db.update(row["id"], status="needs_attention", attention=att)
    req_db.add_event(row["id"], "needs_attention", {"reason": reason, "detail": detail,
                                                     "candidates": candidates or []})
    orchestrate.broadcast(row)
    if notify:
        await dispatch.request_event(row, "request_needs_attention", detail)
    return row


async def try_match(row: dict) -> matching.Match:
    lidarr_album = None
    if row.get("lidarr_album_id"):
        try:
            lidarr_album = await lidarr.get_album(row["lidarr_album_id"])
        except httpx.HTTPError:
            pass
    paths = await _paths_for(row)
    return matching.match(row, lib_db.all_albums(), lidarr_album=lidarr_album, paths=paths)


async def _observe_scans() -> bool:
    """Poll Navidrome's scan status; refresh the index when a scan finished. True if a refresh
    happened."""
    try:
        st = await navidrome.scan_status()
    except (httpx.HTTPError, NavidromeError, ValueError) as e:
        log.debug("scan status unavailable: %s", e)
        return False
    _state["scanning"] = bool(st.get("scanning"))
    last = st.get("lastScan")
    if st.get("scanning") or last == _state["last_scan_seen"]:
        return False
    first = _state["last_scan_seen"] is None
    _state["last_scan_seen"] = last
    if first and lib_db.count():
        return False
    await index.refresh()
    return True


async def _maybe_request_scan(rows: list[dict]) -> None:
    grace = config.settings.scan_watcher_grace_seconds
    if _state["scanning"] or time.monotonic() - _state["scan_requested_at"] < grace * 2:
        return
    due = [r for r in rows if r["status"] == "scanning"
           and (seconds_since(r.get("scan_started_at")) or 0) >= grace]
    if not due:
        return
    try:
        await navidrome.start_scan()
        _state["scan_requested_at"] = time.monotonic()
        for r in due:
            req_db.add_event(r["id"], "scan_requested", None)
        log.info("asked Navidrome to scan for %d request(s)", len(due))
    except (httpx.HTTPError, NavidromeError) as e:
        log.warning("startScan failed: %s", e)


async def tick() -> None:
    rows = waiting()
    if not rows:
        return
    await _observe_scans()
    still = []
    for row in rows:
        m = await try_match(row)
        if m.matched:
            # Into the requester's own library (#27); available once Navidrome has indexed it there.
            store_album = lib_db.get(m.navidrome_album_id) or {}
            ready, mine = await ownership.claim(row.get("user_id"), store_album)
            if not ready:
                still.append(row)
                continue
            if mine:
                m.navidrome_album_id = mine
            await mark_available(row, m)
            continue
        if m.ambiguous:
            await attention(row, "ambiguous_match",
                            "Imported, but more than one album in Navidrome could be it.",
                            m.candidates)
            continue
        still.append(row)
        if row["status"] == "scanning" and \
                (seconds_since(row.get("scan_started_at")) or 0) > config.settings.scan_timeout_seconds:
            await attention(row, "import_not_visible",
                            "Imported by Lidarr but not visible in Navidrome yet.")
    if still:
        await _maybe_request_scan(still)


def enter_scanning(row: dict, reason: str) -> dict:
    """Lidarr has all the files (status loop) or reported an import (webhook)."""
    if row["status"] in ("scanning", "available"):
        return row
    fields = {"status": "scanning", "still_looking": False, "search_cycles": 0}
    if not row.get("scan_started_at"):
        fields["scan_started_at"] = now()
    # Every file is in, so any milestone not seen yet (missed between polls) happened by now.
    for key in ("download_started_at", "first_file_at"):
        if not row.get(key):
            fields[key] = now()
    prev = row["status"]
    row = req_db.update(row["id"], **fields)
    req_db.add_event(row["id"], "status", {"from": prev, "to": "scanning", "by": reason})
    return row
