"""GET /v1/health — 503 when continuo itself is unwell (database, or a dead/stale loop); upstream
outages are reported in the body (status "degraded") but stay 200, so Uptime Kuma and the Docker
healthcheck flag continuo's own failures, not Lidarr's."""
import asyncio
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..db.conn import ping as db_ping
from ..upstream import lidarr, navidrome, slskd
from ..workers import registry

router = APIRouter()

_UPSTREAM_TTL = 30.0
_upstream_cache: tuple[float, dict] = (0.0, {})


async def upstream_status() -> dict:
    global _upstream_cache
    if time.monotonic() - _upstream_cache[0] < _UPSTREAM_TTL and _upstream_cache[1]:
        return _upstream_cache[1]
    checks = {"navidrome": navidrome.ping(), "lidarr": lidarr.ping()}
    if slskd.enabled():
        checks["slskd"] = slskd.ping()
        # slskd can be up and off the Soulseek network: Play now and song requests then fail.
        checks["soulseek"] = slskd.logged_in()
    results = await asyncio.gather(*checks.values(), return_exceptions=True)
    out = {name: ("ok" if r is True else "down") for name, r in zip(checks, results, strict=True)}
    _upstream_cache = (time.monotonic(), out)
    return out


@router.get("/", include_in_schema=False)
async def root() -> dict:
    """What a browser sees at continuo's address: the API lives under /v1."""
    return {"service": "continuo", "health": "/v1/health"}


@router.get("/v1/health", tags=["health"])
async def health(request: Request) -> JSONResponse:
    db_ok = db_ping()
    loops = registry.snapshot()
    loops_ok = not any(v["stale"] for v in loops.values())
    upstream = await upstream_status()
    if not (db_ok and loops_ok):
        status, code = "unhealthy", 503
    elif any(v != "ok" for v in upstream.values()):
        status, code = "degraded", 200
    else:
        status, code = "ok", 200
    if request.headers.get("x-forwarded-for"):
        # Through Caddy, i.e. possibly from the internet: whether it's up, not what's behind it.
        # health-push and the container's own check ask directly and get the whole picture.
        return JSONResponse(status_code=code, content={"status": status})
    return JSONResponse(status_code=code, content={
        "status": status,
        "db": db_ok,
        "loops": {name: ("dead" if v["state"] == "dead" else registry.human(v["ageSeconds"]))
                  for name, v in loops.items()},
        "upstream": upstream,
        "version": __version__,
    })
