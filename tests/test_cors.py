"""CORS for browser apps (the video web app), opt-in, never with credentials."""
import os

from fastapi.testclient import TestClient

from continuo import config


def _app(origins: str):
    from continuo.main import create_app
    config.reload(dict(os.environ, CORS_ALLOW_ORIGINS=origins))
    return TestClient(create_app(disable_workers=True))


def test_off_by_default(client):
    r = client.options("/v1/health", headers={"Origin": "https://tv.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in r.headers


def test_a_preflight_is_answered_without_credentials(fake):
    with _app("*") as c:
        r = c.options("/v1/video/search", headers={"Origin": "file://", "Access-Control-Request-Method": "GET",
                                                    "Access-Control-Request-Headers": "authorization"})
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
        assert "access-control-allow-credentials" not in r.headers
        assert c.get("/v1/health", headers={"Origin": "https://tv.example"}).headers["access-control-allow-origin"] == "*"


def test_listed_origins_only(fake):
    with _app("https://watch.example") as c:
        ok = c.get("/v1/health", headers={"Origin": "https://watch.example"})
        other = c.get("/v1/health", headers={"Origin": "https://evil.example"})
        assert ok.headers["access-control-allow-origin"] == "https://watch.example"
        assert "access-control-allow-origin" not in other.headers
