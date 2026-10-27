"""Picking one song's best copy from slskd search responses (issue #24)."""
from continuo.fetch import soulseek

FORMATS = [("flac", 0), ("mp3", 320)]


def _resp(user, files, free=True, queue=0, speed=1_000_000):
    return {"username": user, "hasFreeUploadSlot": free, "queueLength": queue, "uploadSpeed": speed, "files": files}


def _f(name, ext=None, kbps=None, length=None, size=10):
    return {"filename": name, "extension": ext or name.rsplit(".", 1)[-1], "bitRate": kbps, "length": length, "size": size}


RESPONSES = [
    _resp("slow-flac", [_f(r"@@a\Music\Peach Pit\You and Your Friends\03 - Shampoo Bottles.flac", length=212)], free=False, queue=40),
    _resp("fast-mp3", [_f(r"@@b\Peach Pit\You and Your Friends\03 Shampoo Bottles.mp3", kbps=320, length=213)], speed=9_000_000),
    _resp("low-mp3", [_f(r"@@c\Peach Pit\Shampoo Bottles.mp3", kbps=192, length=213)]),
    _resp("live", [_f(r"@@d\Peach Pit\Live\Shampoo Bottles (Live).flac", length=230)]),
    _resp("other-artist", [_f(r"@@e\Someone\Shampoo Bottles.flac", length=212)]),
    _resp("flac-free", [_f(r"@@f\Peach Pit - You and Your Friends\Peach Pit - Shampoo Bottles.flac", length=211)], queue=2),
]


def test_library_policy_takes_the_preferred_format_first():
    c = soulseek.best(RESPONSES, "Peach Pit", "Shampoo Bottles", 212_000, soulseek.LIBRARY, FORMATS)
    assert c.username == "flac-free"  # FLAC, with a free slot, beats the faster MP3 and the queued FLAC


def test_fastest_policy_takes_the_quickest_good_copy():
    c = soulseek.best(RESPONSES, "Peach Pit", "Shampoo Bottles", 212_000, soulseek.FASTEST, FORMATS)
    assert c.username == "fast-mp3"


def test_wrong_artist_low_bitrate_and_other_versions_are_skipped():
    only_bad = [r for r in RESPONSES if r["username"] in ("low-mp3", "other-artist", "live")]
    assert soulseek.best(only_bad, "Peach Pit", "Shampoo Bottles", 212_000, soulseek.LIBRARY, FORMATS) is None
    # A live version is fine when that's what was asked for.
    c = soulseek.best(only_bad, "Peach Pit", "Shampoo Bottles (Live)", None, soulseek.LIBRARY, FORMATS)
    assert c.username == "live"


def test_a_different_length_is_a_different_recording():
    far = [_resp("x", [_f(r"@@x\Peach Pit\Shampoo Bottles.flac", length=400)])]
    assert soulseek.best(far, "Peach Pit", "Shampoo Bottles", 212_000, soulseek.LIBRARY, FORMATS) is None


def test_download_lands_under_the_remote_parent_folder():
    c = soulseek.candidates([RESPONSES[1]])[0]
    assert c.folder == "You and Your Friends" and c.basename == "03 Shampoo Bottles.mp3"


def test_formats_follow_soularr_when_there_is_no_config():
    assert soulseek.formats() == [("flac", 0), ("mp3", 320)]


SLSKD = "http://slskd.test/api/v0"


def _slskd(fake, monkeypatch, complete_after: int):
    """A slskd whose search reports complete only on the [complete_after]th look (or once
    stopped), and has its responses only then."""
    import dataclasses
    import uuid

    import httpx

    from continuo import config
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, slskd_url="http://slskd.test",
                                                                slskd_api_key="k"))
    monkeypatch.setattr(soulseek, "POLL_S", 0.0)
    monkeypatch.setattr(uuid, "uuid4", lambda: "sid")
    seen = {"looks": 0, "calls": [], "stopped": False}

    def done():
        return seen["looks"] >= complete_after or seen["stopped"]

    def handle(req: httpx.Request):
        seen["calls"].append(req.method)
        if req.method == "PUT":
            seen["stopped"] = True
        if req.method == "GET" and req.url.path.endswith("/responses"):
            return httpx.Response(200, json=RESPONSES if done() else [])
        if req.method == "GET":
            seen["looks"] += 1
            return httpx.Response(200, json={"isComplete": done()})
        return httpx.Response(200, json={})
    fake.on("POST", f"{SLSKD}/searches", handler=handle)
    for method in ("GET", "PUT", "DELETE"):
        fake.on(method, f"{SLSKD}/searches/sid", handler=handle)
    fake.on("GET", f"{SLSKD}/searches/sid/responses", handler=handle)
    return seen


def test_a_long_search_is_waited_for_not_deleted_mid_flight(fake, monkeypatch):
    """slskd ends a search after 15 s without a new response, so a popular one runs ~45-50 s; the
    old 30 s wait deleted it mid-flight: no responses, and a database error in slskd."""
    import asyncio
    seen = _slskd(fake, monkeypatch, complete_after=40)   # the old 30 s wait looked ~30 times
    out = asyncio.run(soulseek.search("Alabaster dePlume Visit Croatia"))
    assert len(out) == len(RESPONSES)
    assert "PUT" not in seen["calls"] and seen["calls"][-1] == "DELETE", "deleted only after it finished"


def test_a_search_past_the_cap_is_stopped_and_keeps_its_results(fake, monkeypatch):
    import asyncio
    seen = _slskd(fake, monkeypatch, complete_after=10**6)
    monkeypatch.setattr(soulseek, "MAX_SEARCH_S", 0.0)
    out = asyncio.run(soulseek.search("anything"))
    assert seen["calls"].index("PUT") < seen["calls"].index("DELETE"), "stopped, then cleaned up"
    assert out == RESPONSES


def test_queries_route_around_a_blocked_artist_name():
    """Soulseek returns nothing for "Kendrick Lamar"; "PRIDE Lamar" finds the song."""
    assert soulseek.queries("Kendrick Lamar", "PRIDE.") == ["Kendrick Lamar PRIDE", "PRIDE Lamar", "Kendrick PRIDE", "PRIDE"]
    assert soulseek.queries("ABBA", "Waterloo") == ["ABBA Waterloo", "Waterloo"]
    assert soulseek.queries("", "Gemini") == ["Gemini"]


def test_search_any_takes_the_first_search_that_answers(monkeypatch):
    import asyncio
    asked = []

    async def search(text, **_kw):
        asked.append(text)
        return [] if "Kendrick Lamar" in text else [{"username": "u", "files": []}]
    monkeypatch.setattr(soulseek, "search", search)
    out = asyncio.run(soulseek.search_any(soulseek.queries("Kendrick Lamar", "PRIDE.")))
    assert asked == ["Kendrick Lamar PRIDE", "PRIDE Lamar"] and out
