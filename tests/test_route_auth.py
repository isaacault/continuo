"""continuo is reachable from the internet (continuo.ault.me): every route needs a signed-in user
unless it's listed here, with why. A new route without auth fails this test."""
from fastapi.routing import APIRoute

from continuo.auth.deps import require_user

# Path -> why it answers without a user.
PUBLIC = {
    "/": "says what continuo is",
    "/v1/health": "up or not (detail only for direct calls, not through Caddy)",
    "/v1/auth/login": "signing in",
    "/v1/auth/refresh": "checks the refresh token itself",
    "/v1/artwork": "cover images in <img> tags; allow-listed sources, rate-limited",
    "/v1/events": "SSE: checks the token itself (Bearer, or ?token= for EventSource)",
    "/v1/play-now/{stream_id}/audio": "checks the token itself (Bearer, or a speaker's signed link: stream, device, expiry)",
    "/rest/getOpenSubsonicExtensions": "OpenSubsonic requires it unauthenticated",
    "/rest/getOpenSubsonicExtensions.view": "the same, by its .view name",
    "/internal/auth/subsonic": "Caddy's forward_auth; never routed from outside (Caddy answers 404)",
    "/internal/webhooks/lidarr": "Lidarr, with the webhook secret; Docker network only",
    "/internal/events/search": "torrent-cleanup, with the webhook secret; Docker network only",
}


def _needs_user(dependant) -> bool:
    return any(d.call is require_user or _needs_user(d) for d in dependant.dependencies)


def _api_routes(routes):
    """Every APIRoute, also inside included routers (newer FastAPI wraps each in _IncludedRouter)."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif getattr(r, "original_router", None) is not None:
            yield from _api_routes(r.original_router.routes)


def test_every_route_needs_a_user_unless_listed(client):
    routes = list(_api_routes(client.app.routes))
    assert len(routes) > 50, "the walk must see the real routes, or this test checks nothing"
    open_routes = sorted({r.path for r in routes if not _needs_user(r.dependant)})
    assert "/v1/auth/login" in open_routes, "sanity: login is open"
    unexpected = [p for p in open_routes if p not in PUBLIC]
    assert unexpected == [], f"routes without auth (add Depends(require_user), or list them in PUBLIC): {unexpected}"
    stale = sorted(p for p in PUBLIC if p not in open_routes)
    assert stale == [], f"listed as public but they need a user now (drop them from PUBLIC): {stale}"


def test_the_api_docs_are_off(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_health_through_the_proxy_says_only_whether_it_is_up(client):
    direct = client.get("/v1/health").json()
    proxied = client.get("/v1/health", headers={"X-Forwarded-For": "203.0.113.9"}).json()
    assert "upstream" in direct and set(proxied) == {"status"}
