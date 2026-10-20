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
    from continuo.db import migrate
    from continuo.upstream import lidarr, slskd
    from continuo.workers import registry
    migrate.run()
    lidarr._album_cache.clear()
    slskd._transfers_cache = (0.0, None)
    registry.reset()
    yield f
    http.use_transport(None)


@pytest.fixture
def client(fake):
    from fastapi.testclient import TestClient

    from continuo.main import create_app
    with TestClient(create_app(disable_workers=True)) as c:
        yield c


def run(coro):
    return asyncio.run(coro)


LIDARR = "http://lidarr.test/api/v1"
ND = "http://navidrome.test"
