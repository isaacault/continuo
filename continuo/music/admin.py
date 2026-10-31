"""Music's admin endpoints: the Lidarr/Navidrome policy check and a library index refresh. Mounted
with the music part (core's admin keeps users, settings, stats and latency)."""
import logging

import httpx
from fastapi import APIRouter, Depends

from .. import config
from ..auth.deps import CurrentUser, require_admin
from ..db import library as lib_db
from ..upstream import lidarr, navidrome
from ..workers import index

log = logging.getLogger("continuo")

router = APIRouter(prefix="/v1/admin", tags=["admin"])


def _check(name: str, status: str, message: str, **extra) -> dict:
    return {"name": name, "status": status, "message": message, **extra}


async def policy_check() -> dict:
    """The constraints of PLAN.md §2.10 checked against the live services. status per check:
    ok | warn | unknown (couldn't be checked)."""
    s = config.settings
    checks = []

    try:
        qp = await lidarr.quality_profile(s.quality_profile_id)
        if qp.get("upgradeAllowed"):
            checks.append(_check("quality_profile_upgrades", "warn",
                                 f"Lidarr quality profile '{qp.get('name')}' allows upgrades — "
                                 "imported music could be replaced. Turn 'Upgrades Allowed' off."))
        else:
            checks.append(_check("quality_profile_upgrades", "ok",
                                 f"Quality profile '{qp.get('name')}' does not upgrade."))
    except httpx.HTTPError as e:
        checks.append(_check("quality_profile_upgrades", "unknown", f"Lidarr unreachable: {e}"))

    try:
        following = [a.get("artistName") for a in await lidarr.all_artists()
                     if a.get("monitorNewItems") not in (None, "none")]
        checks.append(_check(
            "artists_following_new_releases", "warn" if following else "ok",
            f"{len(following)} artist(s) auto-monitor new releases." if following
            else "No artist auto-monitors new releases.", artists=following[:50]))
    except httpx.HTTPError as e:
        checks.append(_check("artists_following_new_releases", "unknown", str(e)))

    try:
        # Lidarr must NOT write tags: it edits files in place, and a torrent's library file is a
        # hardlink of the file still seeding, so tagging would corrupt the torrent. (It would also
        # change Navidrome's album/track IDs, which come from musicbrainz_albumid/trackid.)
        # Matching doesn't need tags: folder paths come first. See docs/RUNBOOK.md.
        meta = await lidarr.metadata_provider_config()
        write, scrub = meta.get("writeAudioTags"), meta.get("scrubAudioTags")
        ok = write in ("no", "No", None) and not scrub
        checks.append(_check(
            "lidarr_tagging", "ok" if ok else "warn",
            "Lidarr doesn't write tags (correct: files are hardlinks of seeding torrents)." if ok
            else f"Lidarr is set to write tags (writeAudioTags={write}, scrubAudioTags={scrub}) — "
                 "it edits files in place, corrupting hardlinked seeding torrents and changing "
                 "Navidrome's album/track IDs. Set 'Tag Audio Files' to No.",
            writeAudioTags=write, scrubAudioTags=scrub))
    except httpx.HTTPError as e:
        checks.append(_check("lidarr_tagging", "unknown", str(e)))

    try:
        hooks = [n for n in await lidarr.notifications() if n.get("implementation") == "Webhook"]
        ours = [n for n in hooks if any(
            f.get("name") == "url" and "/internal/webhooks/lidarr" in str(f.get("value") or "")
            for f in n.get("fields", []))]
        checks.append(_check("lidarr_webhook", "ok" if ours else "warn",
                             "Lidarr webhook to continuo is configured." if ours else
                             "No Lidarr webhook points at continuo (requests still complete via "
                             "polling, just slower)."))
    except httpx.HTTPError as e:
        checks.append(_check("lidarr_webhook", "unknown", str(e)))

    nd_ok = await navidrome.ping()
    mode = s.navidrome_auth
    checks.append(_check(
        "navidrome_auth", "ok" if nd_ok else "warn",
        f"Navidrome reachable with {mode} auth." if nd_ok else
        f"Navidrome unreachable or rejected continuo's {mode} auth"
        + (" — is continuo's address in ND_EXTAUTH_TRUSTEDSOURCES?" if mode == "header" else ".")))

    checks.append(_check("library_index", "ok" if lib_db.count() else "warn",
                         f"{lib_db.count()} albums indexed ({index.last_mode or 'not yet refreshed'})."))
    return {"ok": not any(c["status"] == "warn" for c in checks), "checks": checks}


@router.get("/policy-check")
async def get_policy_check(_admin: CurrentUser = Depends(require_admin)) -> dict:
    return await policy_check()


@router.post("/library/refresh")
async def refresh_library(_admin: CurrentUser = Depends(require_admin)) -> dict:
    return await index.refresh()
