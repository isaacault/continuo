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


def test_refresh_rotates_and_reuse_revokes_device(client):
    make_user()
    s = login(client)
    r1 = client.post("/v1/auth/refresh", json={"refreshToken": s["refreshToken"]})
    assert r1.status_code == 200
    s2 = r1.json()
    assert s2["refreshToken"] != s["refreshToken"]
    assert s2["library"]["password"] == s["library"]["password"]   # same device, same secret
    # The old token again: theft → the whole device is revoked.
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
