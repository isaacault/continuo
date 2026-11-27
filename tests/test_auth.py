import hashlib

from conftest import bearer, login, make_user


def test_login_returns_session_and_facade_credential(client):
    make_user()
    s = login(client)
    assert s["expiresIn"] == 3600 and s["refreshToken"].startswith("crt_")
    assert s["user"] == {"id": 1, "username": "isaac", "role": "admin"}
    lib = s["library"]
    assert lib["type"] == "subsonic" and lib["baseUrl"] == "https://music-api.test"
    assert lib["username"] == "isaac" and len(lib["password"]) >= 24
    me = client.get("/v1/me", headers=bearer(s)).json()
    assert me["device"]["id"] == s["device"]["id"] and me["device"]["current"] is True
    assert client.get("/v1/me/library", headers=bearer(s)).json() == lib


def test_speakers_get_the_receiver_address_when_one_is_set(client, monkeypatch):
    import dataclasses

    from continuo import config
    make_user()
    assert "receiverBaseUrl" not in login(client)["library"], "none unless configured"
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, receiver_base_url="http://192.168.0.101"))
    assert login(client)["library"]["receiverBaseUrl"] == "http://192.168.0.101"


def test_bad_login_and_error_envelope(client):
    make_user()
    r = client.post("/v1/auth/login", json={"username": "isaac", "password": "nope"})
    assert r.status_code == 401
    assert r.json() == {"error": {"code": "invalid_credentials",
                                  "message": "Invalid username or password.", "retryable": False}}
    r = client.post("/v1/auth/login", json={"username": "ghost", "password": "nope"})
    assert r.status_code == 401
    assert client.get("/v1/me").json()["error"]["code"] == "missing_token"


def test_login_rate_limited(client):
    make_user()
    for _ in range(10):
        client.post("/v1/auth/login", json={"username": "isaac", "password": "wrong"})
    r = client.post("/v1/auth/login", json={"username": "isaac", "password": "correct-horse"})
    assert r.status_code == 429


def _later(monkeypatch, seconds: int) -> None:
    """Move continuo's token clock forward (the grace window is measured with it)."""
    from datetime import datetime, timedelta, timezone

    from continuo.auth import tokens
    shifted = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    monkeypatch.setattr(tokens, "_now", lambda: shifted)


def test_refresh_retried_after_a_lost_reply_keeps_the_device(client, caplog):
    """#129: the phone's refresh reached continuo but the reply didn't reach the phone, so it
    retries with the token it still has. That is a retry, not theft: the device stays signed in."""
    make_user()
    s = login(client)
    lost = client.post("/v1/auth/refresh", json={"refreshToken": s["refreshToken"]}).json()
    retry = client.post("/v1/auth/refresh", json={"refreshToken": s["refreshToken"]})
    assert retry.status_code == 200
    s2 = retry.json()
    assert s2["refreshToken"] not in (s["refreshToken"], lost["refreshToken"])
    assert s2["device"]["id"] == s["device"]["id"]
    assert s2["library"]["password"] == s["library"]["password"]   # playback keeps working
    assert client.get("/v1/me", headers=bearer(s2)).status_code == 200
    # The retry's token goes on rotating as usual; the original token is dead now.
    s3 = client.post("/v1/auth/refresh", json={"refreshToken": s2["refreshToken"]})
    assert s3.status_code == 200
    r = client.post("/v1/auth/refresh", json={"refreshToken": s["refreshToken"]})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_refresh_token"
    assert client.get("/v1/me", headers=bearer(s3.json())).status_code == 200
    assert "retried a refresh" in caplog.text


def test_reused_refresh_token_after_the_window_revokes_and_says_so(client, monkeypatch, caplog):
    make_user()
    s = login(client, device="Pixel")
    s2 = client.post("/v1/auth/refresh", json={"refreshToken": s["refreshToken"]}).json()
    _later(monkeypatch, seconds=61)
    r = client.post("/v1/auth/refresh", json={"refreshToken": s["refreshToken"]})
    assert r.status_code == 401 and r.json()["error"]["code"] == "refresh_token_reused"
    assert client.get("/v1/me", headers=bearer(s2)).json()["error"]["code"] == "device_revoked"
    assert any(rec.levelname == "WARNING" and s["device"]["id"] in rec.getMessage()
               and "Pixel" in rec.getMessage() for rec in caplog.records)


def test_a_device_without_a_recorded_rotation_has_no_grace(client):
    """Devices refreshed before the migration have no refresh_rotated_at: reuse still revokes."""
    from continuo.db.conn import connect
    make_user()
    s = login(client)
    s2 = client.post("/v1/auth/refresh", json={"refreshToken": s["refreshToken"]}).json()
    with connect() as c:
        c.execute("UPDATE devices SET refresh_rotated_at=NULL")
    r = client.post("/v1/auth/refresh", json={"refreshToken": s["refreshToken"]})
    assert r.json()["error"]["code"] == "refresh_token_reused"
    assert client.get("/v1/me", headers=bearer(s2)).json()["error"]["code"] == "device_revoked"


def test_logout_is_logged(client, caplog):
    make_user()
    s = login(client)
    assert client.post("/v1/auth/logout", headers=bearer(s)).status_code == 200
    assert f"signed device {s['device']['id']} out" in caplog.text


def test_refresh_rotates_and_reuse_revokes_device(client, monkeypatch):
    make_user()
    s = login(client)
    r1 = client.post("/v1/auth/refresh", json={"refreshToken": s["refreshToken"]})
    assert r1.status_code == 200
    s2 = r1.json()
    assert s2["refreshToken"] != s["refreshToken"]
    assert s2["library"]["password"] == s["library"]["password"]   # same device, same secret
    # The old token again, well after the rotation: theft → the whole device is revoked.
    _later(monkeypatch, seconds=61)
    r2 = client.post("/v1/auth/refresh", json={"refreshToken": s["refreshToken"]})
    assert r2.json()["error"]["code"] == "refresh_token_reused"
    assert client.get("/v1/me", headers=bearer(s2)).json()["error"]["code"] == "device_revoked"
    r3 = client.post("/v1/auth/refresh", json={"refreshToken": s2["refreshToken"]})
    assert r3.status_code == 401


def test_logout_and_revoke_other_device(client):
    make_user()
    phone, laptop = login(client, device="Pixel"), login(client, device="Laptop")
    devices = client.get("/v1/devices", headers=bearer(phone)).json()
    assert {d["name"] for d in devices} == {"Pixel", "Laptop"}
    assert client.delete(f"/v1/devices/{laptop['device']['id']}",
                         headers=bearer(phone)).status_code == 200
    assert client.get("/v1/me", headers=bearer(laptop)).status_code == 401
    assert client.post("/v1/auth/logout", headers=bearer(phone)).status_code == 200
    assert client.get("/v1/me", headers=bearer(phone)).status_code == 401


def test_password_change_ends_other_sessions(client):
    make_user()
    a, b = login(client, device="A"), login(client, device="B")
    r = client.put("/v1/me/password", headers=bearer(a),
                   json={"currentPassword": "correct-horse", "newPassword": "battery-staple"})
    assert r.status_code == 200
    assert client.get("/v1/me", headers=bearer(b)).json()["error"]["code"] == "session_expired"
    login(client, password="battery-staple")


def test_admin_only_routes(client):
    make_user()
    make_user("charlie", role="user")
    s = login(client, "charlie")
    assert client.get("/v1/admin/users", headers=bearer(s)).status_code == 403


# --- façade (Caddy forward_auth) ------------------------------------------------------------
def _fwd(client, query: str):
    return client.get("/internal/auth/subsonic",
                      headers={"X-Forwarded-Uri": f"/rest/stream.view?{query}",
                               "X-Forwarded-Method": "GET"})


def test_facade_token_auth_maps_to_navidrome_user(client):
    make_user(navidrome="isaac-nd")
    s = login(client)
    secret = s["library"]["password"]
    t = hashlib.md5((secret + "abc123").encode()).hexdigest()
    r = _fwd(client, f"u=isaac&t={t}&s=abc123&v=1.16.1&c=polyphony&id=xyz")
    assert r.status_code == 200
    assert r.headers["Remote-User"] == "isaac-nd"
    assert r.headers["X-Continuo-Device"] == s["device"]["id"]


def test_facade_plain_and_enc_password(client):
    make_user()
    secret = login(client)["library"]["password"]
    assert _fwd(client, f"u=isaac&p={secret}").status_code == 200
    assert _fwd(client, f"u=isaac&p=enc:{secret.encode().hex()}").status_code == 200


def test_facade_rejects_with_subsonic_error(client):
    make_user()
    login(client)
    r = _fwd(client, "u=isaac&t=deadbeef&s=x&f=json")
    assert r.status_code == 401
    assert r.json()["subsonic-response"]["error"]["code"] == 40
    assert "Remote-User" not in r.headers
    r = _fwd(client, "t=deadbeef&s=x")
    assert r.status_code == 401 and b'code="10"' in r.content
    # The user's real password is NOT a façade credential.
    assert _fwd(client, "u=isaac&p=correct-horse").status_code == 401


def test_facade_revocation_is_immediate(client):
    make_user()
    s = login(client)
    secret = s["library"]["password"]
    assert _fwd(client, f"u=isaac&p={secret}").status_code == 200     # warms the cache
    client.delete(f"/v1/devices/{s['device']['id']}", headers=bearer(s))
    assert _fwd(client, f"u=isaac&p={secret}").status_code == 401


def test_facade_secret_is_per_user(client):
    make_user()
    make_user("charlie", role="user")
    isaac_secret = login(client)["library"]["password"]
    login(client, "charlie")
    assert _fwd(client, f"u=charlie&p={isaac_secret}").status_code == 401
