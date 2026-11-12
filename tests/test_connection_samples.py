"""Each phone's connection samples (polyphony#185 phase 2, #123): kept per device, shown to admins."""
import time

from conftest import bearer, login, make_user

from continuo.db import connection_samples
from continuo.stream import connection

NOW = int(time.time() * 1000)


def sample(ended_ms=NOW, **over) -> dict:
    """A sample as Polyphony sends it: every field present, the unknown ones null."""
    s = {
        "startedAtMs": ended_ms - 30_000, "endedAtMs": ended_ms, "trigger": "PERIODIC",
        "network": "CELLULAR", "metered": True, "vpn": False, "atHome": False, "responseMs": 180,
        "networkBytes": 480_000, "networkMs": 6_000, "throughputKbps": 640, "cachedBytes": 0,
        "downloadedBytes": 0, "stalls": 0, "stallMs": 0, "serverId": "srv-1",
        "profileId": "adaptive.continuo", "codec": "OPUS", "maxBitrateKbps": 128, "reason": None,
        "capped": False, "cappedMs": 0,
    }
    s.update(over)
    return s


def _post(client, s, device_id, samples):
    return client.post(f"/v1/devices/{device_id}/connection", json={"samples": samples}, headers=bearer(s))


def test_a_device_s_samples_are_stored_and_only_by_its_own_account(client, fake):
    make_user()
    make_user("guest", role="user")
    s = login(client)
    g = login(client, "guest")
    dev = s["device"]["id"]
    r = _post(client, s, dev, [sample(NOW - 60_000), sample(NOW, network="WIFI", atHome=True)])
    assert r.status_code == 200 and r.json()["stored"] == 2
    assert _post(client, g, dev, [sample()]).status_code == 404, "not the guest's device"
    assert _post(client, s, "dev_nope", [sample()]).status_code == 404
    assert client.post(f"/v1/devices/{dev}/connection", json={"samples": []}).status_code == 401
    rows = connection_samples.since(0)
    assert [r["network"] for r in rows] == ["CELLULAR", "WIFI"]
    assert rows[1]["at_home"] == 1 and rows[0]["max_bitrate_kbps"] == 128 and rows[0]["reason"] is None


def test_a_batch_is_never_refused_for_a_value_continuo_doesn_t_know(client, fake):
    """The phone sends a refused batch again forever: an enum a newer Polyphony adds, or a long
    label, is taken (cut short), and only a malformed body or more than 200 is refused."""
    make_user()
    s = login(client)
    dev = s["device"]["id"]
    odd = sample(trigger="SCREEN_OFF", network="SATELLITE", reason="x" * 500, atHome=None,
                 responseMs=None, throughputKbps=None, profileId=None, codec=None, maxBitrateKbps=None)
    assert _post(client, s, dev, [odd]).status_code == 200
    stored = connection_samples.since(0)[0]
    assert stored["network"] == "SATELLITE" and len(stored["reason"]) == 64
    assert _post(client, s, dev, [sample()] * 201).status_code == 422
    missing = sample()
    del missing["stalls"]
    assert _post(client, s, dev, [missing]).status_code == 422


def test_samples_are_kept_a_few_weeks(client, fake):
    make_user()
    s = login(client)
    dev = s["device"]["id"]
    old = NOW - (connection_samples.KEEP_DAYS + 1) * 86_400_000
    assert _post(client, s, dev, [sample(old), sample()]).json()["stored"] == 1
    later = NOW + (connection_samples.KEEP_DAYS + 1) * 86_400_000       # the stored one is too old by then
    newer = NOW + 2 * 86_400_000
    assert connection_samples.add(dev, [sample(newer)], now_ms=later) == 1
    assert [r["ended_at_ms"] for r in connection_samples.since(0)] == [newer], "the older one went"


def test_admin_sees_each_device_s_streaming(client, fake):
    make_user()
    make_user("guest", role="user")
    s = login(client)
    g = login(client, "guest", device="Guest phone")
    dev = s["device"]["id"]
    _post(client, s, dev, [
        sample(NOW - 120_000, network="WIFI", atHome=True, throughputKbps=4000, maxBitrateKbps=320),
        sample(NOW - 90_000, network="CELLULAR", atHome=False, throughputKbps=300, stalls=2, stallMs=4_000,
               maxBitrateKbps=192),
        sample(NOW - 60_000, network="CELLULAR", atHome=False, throughputKbps=None, maxBitrateKbps=128),
        sample(NOW - 30_000, network="CELLULAR", atHome=None, profileId="user,1", maxBitrateKbps=96),
    ])
    _post(client, g, g["device"]["id"], [sample(NOW - 600_000)])
    assert client.get("/v1/admin/connection", headers=bearer(g)).status_code == 403
    r = client.get("/v1/admin/connection", headers=bearer(s))
    assert r.status_code == 200, r.text
    devices = r.json()["devices"]
    assert [d["name"] for d in devices] == ["Pixel 9", "Guest phone"], "streamed from last first"
    d = devices[0]
    assert d["username"] == "isaac" and d["samples"] == 4 and d["minutes"] == 2.0
    assert d["throughputKbps"]["n"] == 3 and d["throughputKbps"]["median"] == 640
    assert d["byNetwork"]["CELLULAR"]["stalls"] == 2 and d["byNetwork"]["WIFI"]["stalls"] == 0
    assert d["byNetwork"]["CELLULAR"]["stallsPerHour"] == 80.0
    assert set(d["byPlace"]) == {"home", "away", "unknown"} and d["byPlace"]["away"]["stalls"] == 2
    assert [lv["kbps"] for lv in d["hlsLevels"]] == [128, 192, 320], "96 wasn't HLS"
    assert d["recent"][0]["hlsLevelKbps"] is None and d["recent"][-1]["hlsLevelKbps"] == 320
    only = client.get(f"/v1/admin/connection?device={dev}", headers=bearer(s)).json()["devices"]
    assert [x["deviceId"] for x in only] == [dev]


def test_summary_of_no_streaming_time_has_no_rate():
    row = {"started_at_ms": 5, "ended_at_ms": 5, "stalls": 0, "stall_ms": 0, "throughput_kbps": None}
    assert connection.summary([row]) == {"samples": 1, "minutes": 0.0, "stalls": 0, "stallMs": 0,
                                         "stallsPerHour": None, "medianKbps": None}
