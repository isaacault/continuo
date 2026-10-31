"""continuo — the server beneath Polyphony."""
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__, config, errors, modules, ratelimit, workers
from .api import admin, artwork, auth, health, notifications
from .auth import passwords
from .db import migrate
from .db import users as users_db
from .upstream import http

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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    migrate.run()
    seed_admin()
    ratelimit.configure()
    tasks = []
    parts = modules.load()
    if not getattr(_app.state, "disable_workers", False):
        tasks = workers.start(parts)
        for part in parts:
            tasks += part.on_start()
    log.info("continuo %s up (parts: core%s; navidrome auth: %s)", __version__,
             "".join(f", {m}" for m in modules.enabled()), config.settings.navidrome_auth)
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await http.aclose()


def create_app(*, disable_workers: bool = False) -> FastAPI:
    docs = config.settings.api_docs
    app = FastAPI(title="continuo", version=__version__, lifespan=lifespan,
                  description="Search once, play what's in the library, request what isn't.",
                  docs_url="/docs" if docs else None, redoc_url="/redoc" if docs else None,
                  openapi_url="/openapi.json" if docs else None)
    app.state.disable_workers = disable_workers
    errors.install(app)

    @app.middleware("http")
    async def _headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    # core's routes, then each enabled part's (a part that's off isn't even imported).
    routers = [health.router, auth.router, notifications.router, admin.router, artwork.router]
    for part in modules.load():
        routers += part.routers()
    for r in routers:
        app.include_router(r)
    return app


setup_logging()
app = create_app()
