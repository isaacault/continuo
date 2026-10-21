# Portions derived from lidseeker (https://github.com/IvoryCobra-VC/lidseeker), MIT License.
"""Status loop (every STATUS_POLL_SECONDS): refresh each live request's pipeline, store the status
it implies, count fruitless search cycles (display-only `stillLooking`, never a give-up), hand
fully-downloaded albums to the scan watcher, and push SSE whenever the pipeline changes — not
only the status."""
import hashlib
import json
import logging

from .. import config
from ..db import requests as req_db
from ..db.conn import now, seconds_since
from ..requests import orchestrate, pipeline
from ..upstream import lidarr
from . import scan

log = logging.getLogger("continuo")

_TRACKED = ("pending", "searching", "downloading", "importing")
_last_hash: dict[int, str] = {}


def _hash(p: dict) -> str:
    return hashlib.sha1(json.dumps(p, sort_keys=True, default=str).encode()).hexdigest()


def count_search_cycle(row: dict, p: dict) -> dict:
    """One fruitless cycle per SEARCH_CYCLE_SECONDS (≈ one Soularr cycle) while genuinely
    searching with nothing in flight; any real progress resets the count. Returns the fields to
    update. (Lidseeker's _track_search_attempts, minus the terminal 'failed'.)"""
    s = config.settings
    if pipeline.in_flight(p):
        if row.get("search_cycles") or row.get("still_looking"):
            return {"search_cycles": 0, "still_looking": False, "last_cycle_at": None}
        return {}
    if p["stage"] != "searching":
        return {}
    elapsed = seconds_since(row.get("last_cycle_at"))
    if elapsed is not None and elapsed < s.search_cycle_seconds:
        return {}
    cycles = (row.get("search_cycles") or 0) + 1
    fields = {"search_cycles": cycles, "last_cycle_at": now()}
    if cycles >= s.still_looking_after_cycles and not row.get("still_looking"):
        fields["still_looking"] = True
    return fields


def latency_milestones(row: dict, p: dict) -> dict:
    """The first time a request is seen downloading, and the first time any audio file is
    complete (a Soulseek file done, or a track imported by Lidarr). Set once, never moved."""
    fields: dict = {}
    source = p.get("source") or row.get("last_source")
    started = p["stage"] in ("downloading", "importing", "scanning", "available")
    first_file = ((p.get("trackFiles") or 0) > 0
                  or ((p.get("download") or {}).get("filesDone") or 0) > 0
                  or p["stage"] in ("scanning", "available"))
    if started and not row.get("download_started_at"):
        fields["download_started_at"] = now()
    if first_file and not row.get("first_file_at"):
        fields["first_file_at"] = now()
        if not fields.get("download_started_at") and not row.get("download_started_at"):
            fields["download_started_at"] = fields["first_file_at"]
    if (fields or started) and source and not row.get("first_source"):
        fields["first_source"] = source
    return fields


async def update_one(row: dict, queue_index: dict) -> dict:
    p = await pipeline.compute(row, queue_index)
    fields: dict = {}
    if p["stage"] == "scanning":
        row = scan.enter_scanning(row, "lidarr_complete")
        p = await pipeline.compute(row, queue_index)
    elif p["stage"] != "requested":
        new_status = pipeline.STAGE_STATUS[p["stage"]]
        if new_status != row["status"]:
            fields["status"] = new_status
            req_db.add_event(row["id"], "status", {"from": row["status"], "to": new_status,
                                                   "detail": p["detail"]})
    if p.get("source") and p["source"] != row.get("last_source"):
        fields["last_source"] = p["source"]
    fields.update(count_search_cycle(row, p))
    fields.update(latency_milestones(row, p))
    if fields.get("still_looking"):
        req_db.add_event(row["id"], "still_looking", {"cycles": fields["search_cycles"]})
    if fields:
        row = req_db.update(row["id"], **fields)
        p = await pipeline.compute(row, queue_index)
    h = _hash(p)
    if _last_hash.get(row["id"]) != h:
        _last_hash[row["id"]] = h
        orchestrate.broadcast(row, p)
    return row


async def tick() -> None:
    rows = [r for r in req_db.list_requests(statuses=_TRACKED) if r.get("lidarr_album_id")]
    if not rows:
        return
    queue_index = await lidarr.queue_index()
    pipeline.forget_queue_items({q["id"] for q in queue_index.values() if q.get("id") is not None})
    for row in rows:
        try:
            await update_one(row, queue_index)
        except Exception:  # noqa: BLE001 — one bad row must not starve the rest
            log.exception("status update failed for request %s", row["id"])
