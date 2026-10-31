"""continuo accounts as Jellyfin users, and per-device Jellyfin tokens (docs/design/video.md §7.1)."""
import os

import httpx
import pytest
from conftest import bearer, login, make_user

from continuo import config

JELLYFIN = "http://jellyfin.test"


@pytest.fixture
def vclient(fake):
    from fastapi.testclient import TestClient

    from continuo.main import create_app
    config.reload(dict(os.environ, CONTINUO_MODULES="music,video", JELLYFIN_URL=JELLYFIN, JELLYFIN_API_KEY="jf-admin",
                       VIDEO_PUBLIC_BASE_URL="https://watch.example"))
    with TestClient(create_app(disable_workers=True)) as c:
        yield c


def _auth_ok(fake, password: str, issued: list):
    def handler(r):
        body = fake.body(r)
        if body["Pw"] != password:
            return httpx.Response(401, json={})
        issued.append(r.headers["Authorization"])
        return {"AccessToken": f"tok-{len(issued)}", "User": {"Id": "jf-u1"}}
    fake.on("POST", f"{JELLYFIN}/Users/AuthenticateByName", handler=handler)


def test_a_new_account_gets_a_jellyfin_user_and_each_device_its_own_token(vclient, fake):
    created = {}
    fake.on("GET", f"{JELLYFIN}/Users", [])
    fake.on("POST", f"{JELLYFIN}/Users/New", handler=lambda r: created.update(fake.body(r)) or {"Id": "jf-u1", "Name": "isaac"})
    issued: list = []
    fake.on("POST", f"{JELLYFIN}/Users/AuthenticateByName",
            handler=lambda r: issued.append(r.headers["Authorization"]) or {"AccessToken": f"tok-{len(issued)}", "User": {"Id": "jf-u1"}})
    make_user()
    s = login(vclient)
    assert s["video"] == {"session": "/v1/video/session"}
    out = vclient.get("/v1/video/session", headers=bearer(s)).json()
    assert out == {"baseUrl": "https://watch.example", "userId": "jf-u1", "accessToken": "tok-1", "deviceId": s["device"]["id"]}
    assert created["Name"] == "isaac" and len(created["Password"]) >= 24, "a random password only continuo knows"
    assert f'DeviceId="{s["device"]["id"]}"' in issued[0]
    assert vclient.get("/v1/video/session", headers=bearer(s)).json()["accessToken"] == "tok-1", "cached, not issued again"
    other = login(vclient, device="Samsung TV")
    assert vclient.get("/v1/video/session", headers=bearer(other)).json()["accessToken"] == "tok-2", "another device, another token"


def test_an_existing_jellyfin_user_is_never_reset_but_linked_by_an_admin(vclient, fake):
    fake.on("GET", f"{JELLYFIN}/Users", [{"Id": "jf-isaac", "Name": "Isaac"}])
    fake.on("DELETE", f"{JELLYFIN}/Devices", handler=lambda r: httpx.Response(204))
    issued: list = []
    _auth_ok(fake, "tv-password", issued)
    make_user()
    s = login(vclient)
    r = vclient.get("/v1/video/session", headers=bearer(s))
    assert r.status_code == 409 and r.json()["error"]["code"] == "jellyfin_link_needed"
    assert not fake.called("POST", f"{JELLYFIN}/Users/New")
    bad = vclient.post("/v1/admin/video/accounts", json={"userId": 1, "jellyfinUsername": "isaac", "jellyfinPassword": "nope"},
                       headers=bearer(s))
    assert bad.status_code == 400
    ok = vclient.post("/v1/admin/video/accounts", json={"userId": 1, "jellyfinUsername": "isaac", "jellyfinPassword": "tv-password"},
                      headers=bearer(s))
    assert ok.status_code == 200 and ok.json() == {"userId": 1, "jellyfinUserId": "jf-isaac", "jellyfinUsername": "Isaac", "managed": False}
    out = vclient.get("/v1/video/session", headers=bearer(s)).json()
    assert out["userId"] == "jf-isaac" and out["accessToken"].startswith("tok-")


def test_signing_a_device_out_signs_it_out_of_jellyfin(vclient, fake):
    fake.on("GET", f"{JELLYFIN}/Users", [])
    fake.on("POST", f"{JELLYFIN}/Users/New", {"Id": "jf-u1", "Name": "isaac"})
    fake.on("POST", f"{JELLYFIN}/Users/AuthenticateByName", {"AccessToken": "tok", "User": {"Id": "jf-u1"}})
    fake.on("DELETE", f"{JELLYFIN}/Devices", handler=lambda r: httpx.Response(204))
    make_user()
    s = login(vclient)
    vclient.get("/v1/video/session", headers=bearer(s))
    assert vclient.post("/v1/auth/logout", headers=bearer(s)).status_code == 200
    deleted = fake.called("DELETE", f"{JELLYFIN}/Devices")
    assert [d.url.params["id"] for d in deleted] == [s["device"]["id"]]
    from continuo.video import accounts
    assert accounts.cached_token(s["device"]["id"]) is None


def test_only_admins_link_accounts(vclient, fake):
    make_user()
    make_user(username="caleb", role="user")
    s = login(vclient, username="caleb")
    r = vclient.post("/v1/admin/video/accounts", json={"userId": 1, "jellyfinUsername": "x", "jellyfinPassword": "y"},
                     headers=bearer(s))
    assert r.status_code == 403
