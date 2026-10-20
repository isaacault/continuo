"""Internal routes (PLAN.md §3.5): Caddy's forward_auth for the Subsonic façade. Caddy must not
expose /internal/auth/* to clients (it would be a credential-checking oracle)."""
import logging
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .. import config
from ..auth import facade

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
