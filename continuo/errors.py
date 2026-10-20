"""One error envelope for every /v1 failure:
{"error": {"code": "lidarr_unavailable", "message": "…", "retryable": true, ...extra}}."""
import logging

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger("continuo")

_DEFAULT_CODES = {
    400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
    405: "method_not_allowed", 409: "conflict", 422: "invalid_request",
    429: "rate_limited", 502: "upstream_unavailable", 503: "unavailable",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.retryable, self.extra = (
            status, code, message, retryable, extra
        )


class UpstreamError(ApiError):
    """A named upstream (lidarr, navidrome, slskd…) failed."""

    def __init__(self, service: str, message: str | None = None):
        super().__init__(502, f"{service}_unavailable",
                         message or f"{service.capitalize()} is temporarily unavailable.", True)


def _respond(status: int, code: str, message: str, retryable: bool,
             extra: dict | None = None, headers: dict | None = None) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "retryable": retryable, **(extra or {})}}
    return JSONResponse(status_code=status, content=body, headers=headers)


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_req: Request, exc: ApiError):
        return _respond(exc.status, exc.code, exc.message, exc.retryable, exc.extra)

    @app.exception_handler(HTTPException)
    async def _http_error(_req: Request, exc: HTTPException):
        return _respond(exc.status_code, _DEFAULT_CODES.get(exc.status_code, "error"),
                        str(exc.detail), exc.status_code >= 500,
                        headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation(_req: Request, exc: RequestValidationError):
        details = [{"loc": list(e.get("loc", ())), "msg": e.get("msg")} for e in exc.errors()]
        return _respond(422, "invalid_request", "Invalid request.", False, {"details": details})

    @app.exception_handler(httpx.HTTPError)
    async def _upstream(req: Request, exc: httpx.HTTPError):
        # An upstream call no handler anticipated: a clean, retryable 502, never a 500.
        service = "upstream"
        url = getattr(getattr(exc, "request", None), "url", None)
        if url is not None:
            from . import config
            s = config.settings
            for name, base in (("lidarr", s.lidarr_url), ("navidrome", s.navidrome_url),
                               ("slskd", s.slskd_url)):
                if str(url).startswith(base):
                    service = name
        log.warning("%s error on %s: %s", service, req.url.path, exc)
        return _respond(502, f"{service}_unavailable",
                        "A music service is temporarily unavailable.", True)
