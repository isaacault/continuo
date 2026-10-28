"""continuo — the server beneath Polyphony."""
import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__, config, errors, ratelimit, workers
from .api import (
    admin,
    artwork,
    auth,
    health,
    internal,
    library,
    notifications,
    playnow,
    requests,
    scrobbling,
    search,
    subsonic,
)
from .auth import passwords
from .db import migrate
from .db import users as users_db
from .requests import orchestrate
from .upstream import http
from .workers import heartbeat, index

log = logging.getLogger("continuo")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname,
               "logger": record.name, "msg": record.getMessage()}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter() if config.settings.log_json else
                         logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Access logs carry ?token= / t= query strings; Caddy keeps the (redacted) access log.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def seed_admin() -> None:
    """First boot only: create ADMIN_USER from ADMIN_PASSWORD when there are no users yet."""
    s = config.settings
    if users_db.count() or not (s.admin_user and s.admin_password):
        return
    users_db.create(s.admin_user, passwords.hash_password(s.admin_password), "admin")
    log.info("created first admin %r", s.admin_user)


async def _startup_checks() -> None:
    try:
        await index.refresh()
    except Exception as e:  # noqa: BLE001 — Navidrome may still be starting
        log.warning("initial library index failed: %s", e)
    try:
        report = await admin.policy_check()
        for c in report["checks"]:
            if c["status"] != "ok":
                log.warning("policy check %s: %s — %s", c["name"], c["status"], c["message"])
    except Exception:  # noqa: BLE001
        log.exception("startup policy check failed")
    heartbeat.mark_nightly_done()   # this was today's index + policy check; don't repeat it


@asynccontextmanager
async def lifespan(_app: FastAPI):
    migrate.run()
    seed_admin()
    ratelimit.configure()
    tasks = []
    if not getattr(_app.state, "disable_workers", False):
        orchestrate.requeue_stranded()
        tasks = workers.start()
        from .requests import playnow
        playnow.requeue_interrupted()
        tasks.append(asyncio.get_running_loop().create_task(_startup_checks()))
    log.info("continuo %s up (navidrome auth: %s)", __version__, config.settings.navidrome_auth)
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await http.aclose()


def create_app(*, disable_workers: bool = False) -> FastAPI:
    app = FastAPI(title="continuo", version=__version__, lifespan=lifespan,
                  description="Search once, play what's in the library, request what isn't.")
    app.state.disable_workers = disable_workers
    errors.install(app)

    @app.middleware("http")
    async def _headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    for r in (health.router, auth.router, search.router, requests.router,
              notifications.router, admin.router, internal.router, artwork.router, library.router,
              playnow.router, scrobbling.router, subsonic.router):
        app.include_router(r)
    return app


setup_logging()
app = create_app()
