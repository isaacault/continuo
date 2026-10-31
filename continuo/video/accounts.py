"""continuo accounts as Jellyfin users, and each device's own Jellyfin token (design §7.1).

A continuo account with no Jellyfin user of the same name gets one, with a random password only
continuo knows. An existing Jellyfin user (someone already watching on the TV with their own
password) is never reset: an admin links it once with its password (POST /v1/admin/video/accounts),
which continuo keeps to issue tokens for new devices. Clients get the token from
GET /v1/video/session and stream from Jellyfin directly; signing a device out of continuo signs it
out of Jellyfin."""
import secrets

import httpx

from .. import config
from ..db.conn import connect, now, row
from ..errors import ApiError
from .upstream import jellyfin


def account(user_id: int) -> dict | None:
    with connect() as c:
        return row(c.execute("SELECT * FROM video_accounts WHERE user_id=?", (user_id,)).fetchone())


def _save_account(user_id: int, jf_user: dict, password: str, managed: bool) -> None:
    with connect() as c:
        c.execute("INSERT INTO video_accounts (user_id, jellyfin_user_id, jellyfin_username, jellyfin_password, managed,"
                  " created_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (user_id) DO UPDATE SET"
                  " jellyfin_user_id=excluded.jellyfin_user_id, jellyfin_username=excluded.jellyfin_username,"
                  " jellyfin_password=excluded.jellyfin_password, managed=excluded.managed",
                  (user_id, jf_user["Id"], jf_user["Name"], password, int(managed), now()))


async def ensure_account(user_id: int, username: str) -> dict:
    """This account's Jellyfin user, created if there's none of that name; an existing one must be
    linked by an admin first (its password is the user's own)."""
    existing = account(user_id)
    if existing:
        return existing
    if await jellyfin.find_user(username):
        raise ApiError(409, "jellyfin_link_needed",
                       f"A Jellyfin user called {username} already exists: an admin links it to this account once "
                       "with its password (POST /v1/admin/video/accounts).")
    password = secrets.token_urlsafe(24)
    _save_account(user_id, await jellyfin.create_user(username, password), password, managed=True)
    return account(user_id)


async def link(user_id: int, jellyfin_username: str, password: str) -> dict:
    """Links an existing Jellyfin user (its password checked by signing in with it)."""
    jf_user = await jellyfin.find_user(jellyfin_username)
    if not jf_user:
        raise ApiError(404, "not_found", f"No Jellyfin user called {jellyfin_username}.")
    try:
        await jellyfin.authenticate(jf_user["Name"], password, f"continuo-link-{user_id}", "continuo link check")
    except httpx.HTTPStatusError as e:
        raise ApiError(400, "wrong_password", "Jellyfin didn't accept that password.") from e
    await jellyfin.delete_device(f"continuo-link-{user_id}")
    _save_account(user_id, jf_user, password, managed=False)
    return account(user_id)


def cached_token(device_id: str) -> str | None:
    with connect() as c:
        r = c.execute("SELECT token FROM video_device_tokens WHERE device_id=?", (device_id,)).fetchone()
    return r[0] if r else None


async def session(user_id: int, username: str, device_id: str, device_name: str) -> dict:
    """What a client needs to stream from Jellyfin: {baseUrl, userId, accessToken, deviceId}."""
    acct = await ensure_account(user_id, username)
    token = cached_token(device_id)
    if token is None:
        issued = await jellyfin.authenticate(acct["jellyfin_username"], acct["jellyfin_password"], device_id, device_name)
        token = issued["token"]
        with connect() as c:
            c.execute("INSERT INTO video_device_tokens (device_id, user_id, token, created_at) VALUES (?, ?, ?, ?)"
                      " ON CONFLICT (device_id) DO UPDATE SET token=excluded.token, created_at=excluded.created_at",
                      (device_id, user_id, token, now()))
    return {"baseUrl": config.settings.video_public_base_url or config.settings.jellyfin_url,
            "userId": acct["jellyfin_user_id"], "accessToken": token, "deviceId": device_id}


async def device_signed_out(device_id: str) -> None:
    with connect() as c:
        had = c.execute("DELETE FROM video_device_tokens WHERE device_id=?", (device_id,)).rowcount
    if had:
        await jellyfin.delete_device(device_id)
