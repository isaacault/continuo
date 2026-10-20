"""continuo — the server beneath Polyphony."""
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__, config, errors, workers
from .api import health
from .db import migrate
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



@asynccontextmanager
async def lifespan(_app: FastAPI):
    migrate.run()
    tasks = [] if getattr(_app.state, "disable_workers", False) else workers.start()
    log.info("continuo %s up", __version__)
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

    app.include_router(health.router)
    return app


setup_logging()
app = create_app()
