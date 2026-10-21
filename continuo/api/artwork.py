"""GET /v1/artwork: catalogue artwork served by continuo (issue #13).

Unauthenticated on purpose: Polyphony loads images with Coil's base OkHttp client, which sends
no auth headers, and music-api.ault.me is only reachable on the LAN and tailnet. What keeps it
from being an open proxy is continuo.artwork's allow-list (sources, redirects), the image-only,
size-capped responses and a rate limit on upstream fetches.
"""
from typing import Optional

import httpx
from fastapi import APIRouter, Query, Request, Response

from .. import artwork
from ..errors import ApiError
from ..ratelimit import artwork_limiter

router = APIRouter(prefix="/v1", tags=["artwork"])

# A cover rarely changes; the URL names the source, so a week of client caching is safe.
_CACHE_CONTROL = "public, max-age=604800"


def _image(request: Request, data: bytes, key: str) -> Response:
    etag = f'"{key[:32]}"'
    headers = {"Cache-Control": _CACHE_CONTROL, "ETag": etag,
               "Content-Security-Policy": "default-src 'none'"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type=artwork.sniff(data), headers=headers)


@router.get("/artwork")
async def get_artwork(request: Request,
                      src: str = Query(..., min_length=8, max_length=2048),
                      size: Optional[int] = Query(None, ge=16, le=4096)) -> Response:
    try:
        url = artwork.check_source(src)
    except artwork.Refused as e:
        raise ApiError(400, "artwork_not_allowed", f"continuo doesn't proxy that URL: {e}.") from e
    url = artwork.sized(url, size)
    key = artwork.cache_key(url)

    data = artwork.cache_get(key)
    if data is not None:
        return _image(request, data, key)
    if artwork.known_missing(key):
        raise ApiError(404, "no_artwork", "There's no artwork for this.")

    artwork_limiter.check(request)
    try:
        data, _kind = await artwork.fetch_image(url)
    except artwork.NotFound as e:
        artwork.remember_missing(key)
        raise ApiError(404, "no_artwork", "There's no artwork for this.") from e
    except (artwork.Refused, artwork.Unusable) as e:
        raise ApiError(502, "artwork_unusable", f"The artwork source misbehaved: {e}.") from e
    except httpx.HTTPError as e:
        raise ApiError(502, "artwork_unavailable", "The artwork source is unreachable.",
                       True) from e
    artwork.cache_put(key, data)
    return _image(request, data, key)
