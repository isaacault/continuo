"""The inbox contract Polyphony relies on to avoid double notifications (issue #16):
deliveredVia on every item and SSE payload, final before the item is visible, and order=desc."""
import asyncio
import json

from conftest import bearer, login, make_user, run

from continuo import config, events
from continuo.db import notifications as notif_db
from continuo.db import requests as req_db
from continuo.notify import dispatch
from continuo.upstream import http

HA = "http://ha.test/api/webhook/music"
RG = "4a1b2c3d-0000-4000-8000-000000000001"


def _ha_on(monkeypatch):
    env = dict(config.settings.__dict__, ha_webhook_url=HA)
    monkeypatch.setattr(config, "settings", config.settings.__class__(**env))
    http.reset()


def _request(user_id=1):
    return req_db.create(mb_release_group_id=RG, title="Geogaddi", artist="Boards of Canada",
                         year=2002, artwork_url=None, user_id=user_id)


def test_poll_during_ha_push_never_sees_item_without_its_ha_status(client, fake, monkeypatch):
    """The race: a client polling while the HA webhook call is in flight must not get the item
    without "homeassistant" (it would post its own notification and the user sees two)."""
    _ha_on(monkeypatch)
    make_user()
    s = login(client)
    in_flight = []

    def ha(_req):
        # A real poll through the API while continuo is waiting on Home Assistant.
        in_flight.append(client.get("/v1/notifications", headers=bearer(s)).json())
        return {}
    fake.on("POST", HA, handler=ha)

    row = _request()
    run(dispatch.request_event(row, "request_available"))
    assert len(in_flight) == 1
    assert in_flight[0]["items"] == [] and in_flight[0]["cursor"] == 0
    after = client.get("/v1/notifications", headers=bearer(s)).json()
    assert [n["deliveredVia"] for n in after["items"]] == [["homeassistant"]]
    # The item appears after the in-flight poll's cursor, so since=cursor still finds it.
    since = client.get(f"/v1/notifications?since={in_flight[0]['cursor']}",
                       headers=bearer(s)).json()
    assert [n["id"] for n in since["items"]] == [after["items"][0]["id"]]


def test_failed_ha_push_leaves_it_to_the_app(client, fake, monkeypatch):
    _ha_on(monkeypatch)
    make_user()
    s = login(client)
    fake.on("POST", HA, {}, status=500)
    run(dispatch.request_event(_request(), "request_needs_attention", "no files"))
    items = client.get("/v1/notifications", headers=bearer(s)).json()["items"]
    assert len(items) == 1 and items[0]["deliveredVia"] == []
    assert fake.called("POST", HA)


def test_ha_disabled_means_no_push_channel(client, fake):
    make_user()
    s = login(client)
    run(dispatch.request_event(_request(), "request_error", "boom"))
    items = client.get("/v1/notifications", headers=bearer(s)).json()["items"]
    assert items[0]["deliveredVia"] == []
    assert not fake.calls


def test_sse_payload_carries_delivered_via(fake, monkeypatch):
    _ha_on(monkeypatch)
    make_user()
    fake.on("POST", HA, {})

    async def go():
        sub = events.subscribe(1, False)
        n = await dispatch.request_event(_request(), "request_available")
        return n, sub.queue.get_nowait()
    n, msg = asyncio.run(go())
    lines = msg.splitlines()
    assert lines[0] == "event: notification.new"
    data = json.loads(lines[1].removeprefix("data: "))
    assert data["id"] == n["id"] and data["deliveredVia"] == ["homeassistant"]
    # The SSE delivery itself is recorded once the fan-out reached a subscriber.
    stored = notif_db.list_for_user(1)[0]
    assert notif_db.delivered_via(stored) == ["homeassistant", "sse"]


def test_order_desc_and_cursor(client, fake):
    make_user()
    s = login(client)
    ids = [notif_db.create(user_id=1, type="request_available", title=f"n{i}", body=None)["id"]
           for i in range(5)]
    notif_db.mark_read(1, [ids[4]])

    def get(q=""):
        r = client.get(f"/v1/notifications{q}", headers=bearer(s))
        assert r.status_code == 200, r.text
        return r.json()

    asc = get("?limit=2")
    assert [n["id"] for n in asc["items"]] == ids[:2] and asc["cursor"] == ids[1]
    assert get() == get("?order=asc")

    desc = get("?order=desc&limit=2")
    assert [n["id"] for n in desc["items"]] == [ids[4], ids[3]]
    assert desc["cursor"] == ids[4]                # highest id returned, as with asc
    assert desc["items"][0]["deliveredVia"] == []
    # since still bounds from below; desc returns the newest above it.
    assert [n["id"] for n in get(f"?order=desc&since={ids[2]}")["items"]] == [ids[4], ids[3]]
    assert [n["id"] for n in get(f"?order=desc&since={ids[1]}&limit=1")["items"]] == [ids[4]]
    # Nothing new: cursor stays at since.
    empty = get(f"?order=desc&since={ids[4]}")
    assert empty["items"] == [] and empty["cursor"] == ids[4]
    unread = get("?order=desc&unread=true&limit=2")
    assert [n["id"] for n in unread["items"]] == [ids[3], ids[2]]
    assert unread["cursor"] == ids[3] and unread["unreadCount"] == 4

    assert client.get("/v1/notifications?order=newest",
                      headers=bearer(s)).status_code == 422
