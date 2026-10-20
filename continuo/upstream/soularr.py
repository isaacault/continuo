# Portions derived from lidseeker (https://github.com/IvoryCobra-VC/lidseeker), MIT License.
"""Soularr: its config.ini (quality preference), its failed-import denylist (for retrying), and
"search now" = restart its container (it has no trigger API) via a locked-down nginx proxy that
allows exactly one Docker operation."""
import configparser
import contextlib
import json
import logging
import os
import tempfile

import httpx

from .. import config
from . import http

log = logging.getLogger("continuo")

# Quality presets → Soularr's allowed_filetypes (most preferred first).
QUALITY_PRESETS = {
    "mp3": "mp3 320,mp3",
    "flac": "flac,mp3 320,mp3",
}


def enabled() -> bool:
    return config.settings.soularr_enabled


def _atomic_write(path: str, write_fn) -> None:
    """Temp file in the same dir + os.replace, so a concurrent reader never sees half a file."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            write_fn(f)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read() -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    cp.read(config.settings.soularr_config_path)
    return cp


def get_quality() -> str | None:
    """'flac' if FLAC is accepted, else 'mp3'; None when the Soularr adapter is off."""
    if not enabled():
        return None
    try:
        types = _read().get("Search Settings", "allowed_filetypes", fallback="")
        return "flac" if "flac" in types.lower() else "mp3"
    except (configparser.Error, OSError) as e:
        log.warning("read Soularr quality failed: %s", e)
        return "mp3"


def set_quality(quality: str) -> bool:
    """Set allowed_filetypes from a preset. True if the file changed."""
    if not enabled():
        raise RuntimeError("Quality control is only available with the Soularr adapter")
    if quality not in QUALITY_PRESETS:
        raise ValueError("quality must be 'mp3' or 'flac'")
    cp = _read()
    if not cp.has_section("Search Settings"):
        raise RuntimeError("Soularr config not available")
    target = QUALITY_PRESETS[quality]
    if cp.get("Search Settings", "allowed_filetypes", fallback="") == target:
        return False
    cp.set("Search Settings", "allowed_filetypes", target)
    _atomic_write(config.settings.soularr_config_path, cp.write)
    return True


def _denylist() -> dict:
    path = config.settings.soularr_denylist_path
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def is_denylisted(album_id: int | None) -> bool:
    if not enabled() or not album_id:
        return False
    return str(album_id) in _denylist()


def clear_denylist_entry(album_id: int | None) -> bool:
    """Remove an album from Soularr's failed-import denylist so it is tried again."""
    if not album_id:
        return False
    data = _denylist()
    if str(album_id) not in data:
        return False
    data.pop(str(album_id))
    _atomic_write(config.settings.soularr_denylist_path, lambda f: json.dump(data, f, indent=2))
    return True


async def trigger_run() -> None:
    """Restart Soularr so it runs a search cycle now."""
    s = config.settings
    c = http.client("docker-proxy", base_url=s.docker_proxy_url, timeout=30.0)
    r = await c.post(f"/containers/{s.soularr_container}/restart")
    # Docker: 204 on success, 304 if already restarting.
    if r.status_code not in (204, 304):
        raise RuntimeError(f"Soularr restart failed ({r.status_code}): {r.text[:200]}")


async def try_trigger_run(context: str) -> bool:
    if not enabled():
        return False
    try:
        await trigger_run()
        return True
    except (httpx.HTTPError, RuntimeError) as e:
        log.warning("%s: Soularr trigger failed: %s", context, e)
        return False
