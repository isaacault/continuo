"""The media cache (issue #23): <MUSIC_DIR>/.continuo-cache/<id>/, on the library's own mount so
an album moves in or out with one rename. Navidrome skips it (a dot-folder, and .ndignore);
Lidarr skips hidden folders. Today it holds albums removed from the library; streamed and
prefetched music will live here too, evicted together, least recently used first."""
import logging
import os
import shutil
from datetime import timedelta

from .. import config
from ..db import cache as cache_db
from ..db.conn import parse_ts

log = logging.getLogger("continuo")

CACHE_FOLDER = ".continuo-cache"


class Unavailable(Exception):
    """MUSIC_DIR isn't set or isn't mounted: nothing can be moved."""


def music_dir() -> str:
    d = config.settings.music_dir
    if not d or not os.path.isdir(d):
        raise Unavailable("MUSIC_DIR is not set or not mounted")
    return os.path.realpath(d)


def cache_dir() -> str:
    d = os.path.join(music_dir(), CACHE_FOLDER)
    if not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, ".ndignore"), "w").close()  # belt and braces for Navidrome
    return d


def size_of(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _expired(item: dict, now_ts) -> bool:
    """A removed album past REMOVED_MAX_DAYS goes whatever the cache size."""
    created = parse_ts(item["created_at"])
    return item["origin"] == "removed" and created is not None \
        and now_ts - created > timedelta(days=config.settings.removed_max_days)


def _protected(item: dict, now_ts) -> bool:
    """A removed album inside its undo window (REMOVED_MIN_DAYS) is never evicted for space."""
    created = parse_ts(item["created_at"])
    return item["origin"] == "removed" and created is not None \
        and now_ts - created < timedelta(days=config.settings.removed_min_days)


def evict() -> list[dict]:
    """Drop expired items, then least recently used ones until the cache fits MEDIA_CACHE_MAX_GB.
    Returns what was dropped."""
    from datetime import datetime, timezone
    now_ts = datetime.now(timezone.utc)
    try:
        base = cache_dir()
    except Unavailable:
        return []
    limit = config.settings.media_cache_max_gb * 1024 ** 3
    total = cache_db.total_bytes()
    dropped = []
    for item in cache_db.least_recently_used():
        if not (_expired(item, now_ts) or (total > limit and not _protected(item, now_ts))):
            continue
        path = os.path.realpath(os.path.join(base, item["cache_path"]))
        if path.startswith(base + os.sep):
            shutil.rmtree(path, ignore_errors=True)
        cache_db.delete(item["id"])
        total -= item["bytes"] or 0
        dropped.append(item)
        log.info("media cache: dropped %s %r (%s)", item["origin"], item["title"], item["kind"])
    return dropped
