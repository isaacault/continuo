"""continuo's parts (docs/design/video.md §3): core always, music only when CONTINUO_MODULES says."""
import os
import sys

import pytest
from conftest import bearer, login, make_user

from continuo import config, modules, workers


def _client_with(parts: str):
    from fastapi.testclient import TestClient

    from continuo.main import create_app
    config.reload(dict(os.environ, CONTINUO_MODULES=parts))
    return TestClient(create_app(disable_workers=True))


def test_music_is_the_default_and_mounts_its_routes(client):
    assert modules.enabled() == ["music"]
    make_user()
    s = login(client)
    assert "library" in s, "the Subsonic credential comes with sign-in"
    assert client.get("/v1/search", params={"q": "x"}, headers=bearer(s)).status_code != 404
    assert client.get("/v1/me/library", headers=bearer(s)).status_code == 200


def test_core_alone_has_no_music_routes_loops_or_credential(fake):
    with _client_with("") as c:
        make_user()
        s = login(c)
        assert "library" not in s and s["accessToken"]
        assert c.get("/v1/health").status_code == 200
        assert c.get("/v1/search", params={"q": "x"}, headers=bearer(s)).status_code == 404
        assert c.get("/rest/ping").status_code == 404
        assert c.get("/v1/me/library", headers=bearer(s)).status_code == 404
    assert list(workers.loop_specs(modules.load())) == ["heartbeat"]


def test_music_brings_its_loops(fake):
    config.reload(dict(os.environ, CONTINUO_MODULES="music"))
    assert set(workers.loop_specs(modules.load())) == {
        "heartbeat", "status", "reconcile", "scan", "nightly", "libraries", "tracks", "discover", "completeness"}


def test_an_unknown_part_is_refused(fake):
    config.reload(dict(os.environ, CONTINUO_MODULES="music,podcasts"))
    with pytest.raises(RuntimeError, match="podcasts"):
        modules.load()


def test_a_part_thats_off_isnt_imported(fake):
    sys.modules.pop("continuo.music", None)
    config.reload(dict(os.environ, CONTINUO_MODULES=""))
    modules.load()
    assert "continuo.music" not in sys.modules


def test_core_alone_loads_no_music_code(fake):
    """The point of parts (design §2): a server without music doesn't carry it. Checked in a fresh
    interpreter, as the test process has music loaded already."""
    import subprocess
    code = ("import sys; from continuo.main import create_app; create_app(disable_workers=True); "
            "print(' '.join(sorted(m for m in sys.modules if m.startswith('continuo.'))))")
    env = dict(os.environ, CONTINUO_MODULES="")
    loaded = set(subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                                check=True).stdout.split())
    music = {"continuo.upstream.lidarr", "continuo.upstream.navidrome", "continuo.upstream.slskd",
             "continuo.upstream.soularr", "continuo.upstream.musicbrainz", "continuo.workers.index",
             "continuo.db.library", "continuo.music", "continuo.search", "continuo.fetch"}
    assert not loaded & music, loaded & music
