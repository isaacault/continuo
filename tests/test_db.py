from continuo.db import migrate
from continuo.db import requests as req_db
from continuo.db.conn import connect


def test_migrations_apply_once(fake):
    assert migrate.run() == []          # the fixture already applied them
    with connect() as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "devices", "requests", "request_events", "library_albums",
            "notifications", "settings", "schema_migrations"} <= tables


def test_request_lifecycle_and_rerequest_resets(fake):
    row = req_db.create(mb_release_group_id="rg-1", title="OK Computer", artist="Radiohead",
                        year=1997, artwork_url=None, user_id=None)
    assert row["status"] == "pending" and row["still_looking"] is False
    req_db.update(row["id"], status="error", error="boom", still_looking=True, search_cycles=9,
                  import_paths=["/a/b.flac"])
    got = req_db.get(row["id"])
    assert got["import_paths"] == ["/a/b.flac"] and got["still_looking"] is True
    again = req_db.create(mb_release_group_id="rg-1", title="OK Computer", artist="Radiohead",
                          year=1997, artwork_url=None, user_id=None)
    assert again["id"] == row["id"]
    assert again["status"] == "pending" and again["error"] is None
    assert again["search_cycles"] == 0 and again["import_paths"] == []


def test_active_excludes_available_and_error(fake):
    a = req_db.create(mb_release_group_id="rg-a", title="A", artist="X", year=None,
                      artwork_url=None, user_id=None)
    b = req_db.create(mb_release_group_id="rg-b", title="B", artist="X", year=None,
                      artwork_url=None, user_id=None)
    req_db.update(b["id"], status="available")
    assert set(req_db.active_by_release_group()) == {"rg-a"}
    req_db.update(a["id"], status="error")
    assert req_db.active_by_release_group() == {}


def test_events_are_ordered(fake):
    row = req_db.create(mb_release_group_id="rg-e", title="E", artist="X", year=None,
                        artwork_url=None, user_id=None)
    req_db.add_event(row["id"], "one", {"n": 1})
    req_db.add_event(row["id"], "two", None)
    assert [e["type"] for e in req_db.events(row["id"])] == ["one", "two"]
    assert req_db.events(row["id"])[0]["detail"] == {"n": 1}
