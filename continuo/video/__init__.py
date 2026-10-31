"""The video part (docs/design/video.md): Jellyfin (the library, streaming), TMDB (the catalogue),
Radarr (films' wanted list); Sonarr comes next. Nothing here is imported unless CONTINUO_MODULES
lists "video", and its tables (continuo/video/migrations, 1000+) exist only then."""
import asyncio
from typing import Callable


def routers() -> list:
    from . import api
    return [api.router, api.admin_router]


def loops() -> dict[str, tuple[Callable[[], float], Callable]]:
    from .. import config
    from . import index, requests
    return {"video_index": (lambda: config.settings.video_index_seconds, index.refresh),
            # Film requests through Radarr to Jellyfin: checked every minute.
            "video_requests": (lambda: 60, requests.tick)}


def on_start() -> list[asyncio.Task]:
    return []


def health_checks() -> dict:
    from .upstream import jellyfin, radarr, sonarr, tmdb
    checks = {"jellyfin": jellyfin.ping(), "radarr": radarr.ping(), "sonarr": sonarr.ping()}
    if tmdb.configured():
        checks["tmdb"] = tmdb.ping()
    return checks


def sign_in(user: dict, device: dict) -> dict:
    """Where this device's Jellyfin session comes from (issued on first ask, as it needs Jellyfin)."""
    return {"video": {"session": "/v1/video/session"}}


async def on_device_signed_out(device_id: str) -> None:
    from . import accounts
    await accounts.device_signed_out(device_id)
