"""Internal routes (PLAN.md §3.5): Caddy's forward_auth for the Subsonic façade, the Lidarr
webhook, and torrent-cleanup's re-search reports. Caddy must not expose /internal/auth/* to
clients (it would be a credential-checking oracle); the webhook routes are secret-protected."""
import asyncio
import base64
import hmac
import logging
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .. import config
from ..auth import facade
from ..db import requests as req_db
from ..db.conn import now
from ..errors import ApiError
from ..requests import orchestrate
from ..upstream import homeassistant
from ..workers import scan
from .schemas import SearchEventIn

log = logging.getLogger("continuo")
router = APIRouter(prefix="/internal", include_in_schema=False)


# --------------------------------------------------------------------------
# Subsonic façade (Caddy forward_auth)
# --------------------------------------------------------------------------
def _subsonic_error(params: dict, code: int, message: str) -> Response:
    """forward_auth relays a non-2xx answer to the client verbatim, so make it a proper Subsonic
    error the client can show ("wrong username or password")."""
    if (params.get("f") or "").lower() in ("json", "jsonp"):
        return JSONResponse(status_code=401, content={"subsonic-response": {
            "status": "failed", "version": "1.16.1", "type": "continuo", "openSubsonic": True,
            "error": {"code": code, "message": message}}})
    body = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<subsonic-response xmlns="http://subsonic.org/restapi" status="failed" '
            f'version="1.16.1"><error code="{code}" message="{message}"/></subsonic-response>')
    return Response(body, status_code=401, media_type="application/xml")


@router.get("/auth/subsonic")
async def subsonic_forward_auth(request: Request) -> Response:
    uri = request.headers.get("x-forwarded-uri") or ""
    params = {k: v[-1] for k, v in parse_qs(urlsplit(uri).query, keep_blank_values=True).items()}
    if not params.get("u"):
        return _subsonic_error(params, 10, "Required parameter is missing: u")
    ident = facade.verify(params)
    if ident is None:
        log.info("façade: rejected %r for %s", params.get("u"), urlsplit(uri).path)
        return _subsonic_error(params, 40, "Wrong username or password")
    return Response(status_code=200, headers={config.settings.navidrome_user_header:
                                              ident.navidrome_username,
                                              "X-Continuo-Device": ident.device_id})


# --------------------------------------------------------------------------
# Webhook auth
# --------------------------------------------------------------------------
def _check_secret(request: Request) -> None:
    secret = config.settings.lidarr_webhook_secret
    if not secret:
        raise ApiError(503, "webhook_disabled", "LIDARR_WEBHOOK_SECRET is not set.")
    given = request.headers.get("x-webhook-secret") or request.query_params.get("secret") or ""
    auth = request.headers.get("authorization", "")
    if not given and auth.lower().startswith("basic "):
        try:
            given = base64.b64decode(auth[6:]).decode().partition(":")[2]
        except ValueError:
            given = ""
    if not hmac.compare_digest(given.encode(), secret.encode()):
        raise ApiError(401, "bad_secret", "Webhook secret missing or wrong.")


def _lower(obj):
    """Lidarr's payload casing has varied between versions (MBId/mbId); compare lowercased."""
    if isinstance(obj, dict):
        return {str(k).lower(): _lower(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_lower(v) for v in obj]
    return obj


# downloadId -> request id, from Grab events, so DownloadFailure (which carries no album)
# can still be attributed. In-memory: a restart only loses the attribution of a failure note.
_downloads: dict[str, int] = {}


def _requests_for(payload: dict) -> list[dict]:
    albums = list(payload.get("albums") or [])
    if payload.get("album"):
        albums.append(payload["album"])
    found: dict[int, dict] = {}
    for a in albums:
        row = req_db.by_lidarr_album(a["id"]) if a.get("id") else None
        if row is None and a.get("mbid"):
            row = req_db.by_release_group(str(a["mbid"]).lower())
        if row is not None:
            found[row["id"]] = row
    did = payload.get("downloadid")
    if not found and did and did in _downloads:
        row = req_db.get(_downloads[did])
        if row:
            found[row["id"]] = row
    return list(found.values())


def _source(payload: dict) -> str:
    kind = f"{payload.get('downloadclienttype') or ''} {payload.get('downloadclient') or ''}".lower()
    return "soulseek" if ("slskd" in kind or "soulseek" in kind) else "torrent"


async def _on_grab(payload: dict, rows: list[dict]) -> None:
    for row in rows:
        if payload.get("downloadid"):
            _downloads[payload["downloadid"]] = row["id"]
            if len(_downloads) > 1000:
                _downloads.pop(next(iter(_downloads)))
        release = payload.get("release") or {}
        req_db.add_event(row["id"], "webhook_grab", {
            "release": release.get("releasetitle"), "indexer": release.get("indexer"),
            "client": payload.get("downloadclient")})
        if row["status"] in ("pending", "searching"):
            updated = req_db.update(row["id"], status="downloading", last_source=_source(payload),
                                    search_cycles=0, still_looking=False)
            req_db.add_event(row["id"], "status", {"from": row["status"], "to": "downloading",
                                                   "by": "webhook"})
            orchestrate.broadcast(updated)


async def _on_import(payload: dict, rows: list[dict]) -> None:
    paths = [f.get("path") for f in payload.get("trackfiles") or [] if f.get("path")]
    album = payload.get("album") or {}
    if payload.get("isupgrade"):
        # Policy: never replace music already in the library. Lidarr did — tell a human.
        title = album.get("title") or "an album"
        artist = (payload.get("artist") or {}).get("name") or ""
        log.warning("Lidarr imported an UPGRADE for %s — %s", title, artist)
        for row in rows:
            await scan.attention(row, "upgrade_detected",
                                 "Lidarr replaced existing files with an upgrade.")
        if not rows:
            await homeassistant.notify(
                title="Lidarr replaced existing music",
                message=f"{title} — {artist}: an upgrade replaced files. Check the quality "
                        "profile's 'Upgrades Allowed'.", tag="continuo-upgrade", kind="policy")
        return
    for row in rows:
        merged = sorted(set(row.get("import_paths") or []) | set(paths))
        req_db.update(row["id"], import_paths=merged, imported_at=now(),
                      last_source=row.get("last_source") or _source(payload))
        req_db.add_event(row["id"], "webhook_import", {"files": len(paths)})
        if row["status"] != "available":
            updated = scan.enter_scanning(req_db.get(row["id"]), "webhook")
            orchestrate.broadcast(updated)
    if rows:
        # Navidrome's watcher usually beats us; check right away rather than at the next tick.
        asyncio.get_running_loop().create_task(_scan_soon())


async def _scan_soon() -> None:
    await asyncio.sleep(5)
    try:
        await scan.tick()
    except Exception:  # noqa: BLE001
        log.exception("scan after webhook failed")


async def _on_failure(event_type: str, payload: dict, rows: list[dict]) -> None:
    message = payload.get("message") or payload.get("releasetitle") or event_type
    for row in rows:
        req_db.add_event(row["id"], f"webhook_{event_type.lower()}", {"message": message})
        if row["status"] in ("downloading", "importing"):
            updated = req_db.update(row["id"], status="searching")
            req_db.add_event(row["id"], "status", {"from": row["status"], "to": "searching",
                                                   "by": "webhook", "detail": message})
            orchestrate.broadcast(updated)


@router.post("/webhooks/lidarr")
async def lidarr_webhook(request: Request) -> dict:
    _check_secret(request)
    try:
        payload = _lower(await request.json())
    except ValueError as e:
        raise ApiError(400, "bad_payload", "Expected JSON.") from e
    event_type = payload.get("eventtype") or ""
    log.info("lidarr webhook: eventType=%s", event_type)
    if event_type == "Test":
        return {"ok": True, "eventType": "Test"}
    rows = _requests_for(payload)
    if event_type == "Grab":
        await _on_grab(payload, rows)
    elif event_type == "Download":
        await _on_import(payload, rows)
    elif event_type in ("DownloadFailure", "ImportFailure"):
        await _on_failure(event_type, payload, rows)
    return {"ok": True, "eventType": event_type, "requests": [r["id"] for r in rows]}


@router.post("/events/search")
async def search_reported(body: SearchEventIn, request: Request) -> dict:
    """torrent-cleanup's daily re-search (rule 10) reports here so the app can show
    'last searched'."""
    _check_secret(request)
    row = req_db.by_lidarr_album(body.lidarrAlbumId)
    if row is None:
        return {"ok": True, "matched": False}
    req_db.update(row["id"], last_search_at=body.at or now())
    req_db.add_event(row["id"], "search_fired", {"by": body.rule or "torrent-cleanup"})
    return {"ok": True, "matched": True, "requestId": row["id"]}
