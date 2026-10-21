"""GET/POST /rest/getOpenSubsonicExtensions for the Subsonic façade (issue #4).

Caddy's forward_auth only sees the request line and headers, never the body, so the façade can
check credentials only from the query string. A client that uses the OpenSubsonic `formPost`
extension (POST, credentials in an x-www-form-urlencoded body) is therefore refused. OpenSubsonic
clients only use an extension the server advertises in getOpenSubsonicExtensions (Feishin, for
one, switches to POST exactly when `formPost` is listed), so continuo answers that endpoint on
music-api.ault.me itself: Navidrome's list, minus the extensions the façade can't honour.

Unauthenticated, as the spec requires ("Unlike all other APIs getOpenSubsonicExtensions must be
publicly accessible"); Navidrome serves it without auth too. Client parameters (credentials
included) are not forwarded: continuo asks Navidrome for JSON and renders the format the client
asked for. Proxying rather than a static list keeps the rest of the list (transcodeOffset,
songLyrics, sonicSimilarity only when configured, ...) true across Navidrome upgrades.
"""
import json
import logging
import re
from urllib.parse import parse_qs
from xml.sax.saxutils import quoteattr

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..upstream import navidrome

log = logging.getLogger("continuo")
router = APIRouter(prefix="/rest", include_in_schema=False)

# Extensions the façade must not advertise:
# - formPost: credentials in the body are invisible to forward_auth.
# - apiKeyAuthentication (Navidrome after 0.64): an `apiKey` instead of u + t/s; the façade
#   authenticates continuo device secrets only, so an apiKey request would be refused.
WITHHELD = frozenset({"formPost", "apiKeyAuthentication"})

_CALLBACK = re.compile(r"^[A-Za-z_$][\w$.]{0,63}$")


async def _params(request: Request) -> dict:
    params = dict(request.query_params)
    if request.method == "POST" and request.headers.get("content-type", "").startswith(
            "application/x-www-form-urlencoded"):
        body = (await request.body()).decode("utf-8", "replace")
        params.update({k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()})
    return params


def _xml(body: dict) -> str:
    attrs = "".join(f" {k}={quoteattr(str(v).lower() if isinstance(v, bool) else str(v))}"
                    for k, v in body.items() if isinstance(v, (str, int, bool)))
    inner = ""
    for ext in body.get("openSubsonicExtensions") or []:
        versions = "".join(f"<versions>{int(v)}</versions>" for v in ext.get("versions") or [])
        inner += f"<openSubsonicExtensions name={quoteattr(str(ext.get('name', '')))}>{versions}</openSubsonicExtensions>"
    err = body.get("error")
    if isinstance(err, dict):
        inner += (f"<error code={quoteattr(str(err.get('code', 0)))} "
                  f"message={quoteattr(str(err.get('message', '')))}/>")
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<subsonic-response xmlns="http://subsonic.org/restapi"{attrs}>{inner}'
            "</subsonic-response>")


def _render(params: dict, body: dict, status: int = 200) -> Response:
    fmt = (params.get("f") or "xml").lower()
    if fmt == "json":
        return JSONResponse({"subsonic-response": body}, status_code=status)
    if fmt == "jsonp":
        callback = params.get("callback") or ""
        if _CALLBACK.match(callback):
            payload = json.dumps({"subsonic-response": body})
            return Response(f"{callback}({payload});", status_code=status,
                            media_type="application/javascript")
        return JSONResponse({"subsonic-response": body}, status_code=status)
    return Response(_xml(body), status_code=status, media_type="application/xml")


@router.api_route("/getOpenSubsonicExtensions", methods=["GET", "POST"])
@router.api_route("/getOpenSubsonicExtensions.view", methods=["GET", "POST"])
async def open_subsonic_extensions(request: Request) -> Response:
    params = await _params(request)
    try:
        body = await navidrome.open_subsonic_extensions()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("getOpenSubsonicExtensions: Navidrome unavailable: %s", e)
        return _render(params, {"status": "failed", "version": "1.16.1", "type": "continuo",
                                "openSubsonic": True,
                                "error": {"code": 0, "message": "Navidrome is unavailable"}},
                       status=502)
    exts = body.get("openSubsonicExtensions")
    if isinstance(exts, list):
        body["openSubsonicExtensions"] = [e for e in exts if isinstance(e, dict)
                                          and e.get("name") not in WITHHELD]
    return _render(params, body)
