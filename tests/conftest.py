import asyncio
import json
import os
import tempfile

import httpx
import pytest

# Configure before continuo is imported anywhere.
_TMP = tempfile.mkdtemp(prefix="continuo-test-")
os.environ.update({
    "DB_PATH": os.path.join(_TMP, "boot.db"),
    "JWT_SECRET": "test-secret-0123456789abcdef0123456789abcdef",
    "LIDARR_URL": "http://lidarr.test",
    "LIDARR_API_KEY": "lk",
    "LIDARR_WEBHOOK_SECRET": "hook-secret",
    "NAVIDROME_URL": "http://navidrome.test",
    "NAVIDROME_AUTH": "header",
    "SLSKD_URL": "http://slskd.test",
    "SLSKD_API_KEY": "",
    "ROOT_FOLDER_PATH": "/data/music",
    "NAVIDROME_MUSIC_ROOT": "/music",
    "PUBLIC_BASE_URL": "https://music-api.test",
    "LOG_JSON": "false",
})

from continuo import config  # noqa: E402
from continuo.upstream import http  # noqa: E402


class FakeUpstream:
    """httpx.MockTransport with routes keyed by (METHOD, url-without-query)."""

    def __init__(self):
        self.routes: dict[tuple[str, str], object] = {}
        self.calls: list[httpx.Request] = []
        self.unmatched: list[str] = []

    def on(self, method: str, url: str, json_body=None, status: int = 200, handler=None,
           headers: dict | None = None):
        if handler is None:
            def handler(_req, _b=json_body, _s=status, _h=headers):
                return httpx.Response(_s, json=_b, headers=_h)
        self.routes[(method.upper(), url)] = handler
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        key = (request.method, str(request.url.copy_with(query=None)))
        handler = self.routes.get(key)
        if handler is None:
            self.unmatched.append(f"{key[0]} {key[1]}")
            return httpx.Response(404, json={"unmatched": key[1]})
        out = handler(request)
        return out if isinstance(out, httpx.Response) else httpx.Response(200, json=out)

    def called(self, method: str, url: str) -> list[httpx.Request]:
        return [c for c in self.calls
                if c.method == method and str(c.url.copy_with(query=None)) == url]

    def body(self, req: httpx.Request):
        return json.loads(req.content or b"null")


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """Fresh DB + config + mocked upstreams for every test."""
    env = dict(os.environ, DB_PATH=str(tmp_path / "continuo.db"))
    monkeypatch.setattr(os, "environ", env)
    config.reload(env)
    f = FakeUpstream()
    http.use_transport(httpx.MockTransport(f))
    from continuo import events
    from continuo.auth import facade
    from continuo.db import migrate
    from continuo.requests import pipeline
    from continuo.search import merge
    from continuo.upstream import lidarr, slskd
    from continuo.workers import registry, scan, status
    migrate.run()
    facade.invalidate_all()
    lidarr._album_cache.clear()
    merge._catalog_cache.clear()
    from continuo.search import ranked
    from continuo.upstream import deezer
    ranked._ids_cache.clear()
    from continuo.search import discover
    discover._page_cache.clear()
    discover._last_page.clear()
    discover._slow_cache.clear()
    discover._history_cache.clear()
    discover._built_at.clear()
    merge._quick_cache.clear()
    deezer.reset()
    pipeline._progress.clear()
    status._last_hash.clear()
    slskd._transfers_cache = (0.0, None)
    scan._state.update({"last_scan_seen": None, "scan_requested_at": 0.0, "scanning": False})
    registry.reset()
    events._subscribers.clear()
    from continuo import ratelimit
    for lim in (ratelimit.login_limiter, ratelimit.search_limiter, ratelimit.catalog_limiter,
                ratelimit.artwork_limiter):
        lim._hits.clear()
    from continuo import artwork
    artwork.reset()
    yield f
    http.use_transport(None)


@pytest.fixture
def client(fake):
    from fastapi.testclient import TestClient

    from continuo.main import create_app
    with TestClient(create_app(disable_workers=True)) as c:
        yield c


def make_user(username="isaac", password="correct-horse", role="admin", navidrome=None):
    from continuo.auth import passwords
    from continuo.db import users
    return users.create(username, passwords.hash_password(password), role, navidrome)


def login(client, username="isaac", password="correct-horse", device="Pixel 9"):
    r = client.post("/v1/auth/login", json={"username": username, "password": password,
                                            "device": {"name": device, "platform": "android"}})
    assert r.status_code == 200, r.text
    return r.json()


def bearer(session) -> dict:
    return {"Authorization": f"Bearer {session['accessToken']}"}


def run(coro):
    return asyncio.run(coro)


LIDARR = "http://lidarr.test/api/v1"
ND = "http://navidrome.test"
