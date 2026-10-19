"""Redis-backed fixed-window rate limiting.

Fixed windows are cheap (one round trip, atomic via Lua) and good enough for abuse protection;
the edge (WAF/API gateway) applies coarser global limits in front of this.
If Redis is down we fail open and log: availability matters more than a perfect limit here,
and login brute force is still bounded by per-account lockout in Postgres.
"""

from collections.abc import Awaitable, Callable

import structlog
from fastapi import Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.errors import TooManyRequests

logger = structlog.get_logger(__name__)

_HIT_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then redis.call('PEXPIRE', KEYS[1], ARGV[1]) end
return {current, redis.call('PTTL', KEYS[1])}
"""


class RateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> None:
        try:
            result = await self._redis.eval(  # type: ignore[misc]
                _HIT_SCRIPT, 1, f"rl:{key}", str(window_seconds * 1000)
            )
            count, ttl_ms = int(result[0]), int(result[1])
        except RedisError:
            logger.warning("rate_limit_unavailable", key_scope=key.split(":", 1)[0])
            return
        if count > limit:
            retry_after = max(1, ttl_ms // 1000)
            raise TooManyRequests(
                "Too many requests, slow down", headers={"Retry-After": str(retry_after)}
            )


def client_ip(request: Request) -> str:
    # uvicorn --proxy-headers rewrites client from X-Forwarded-For only for trusted proxies.
    return request.client.host if request.client else "unknown"


def rate_limit(
    scope: str, *, limit: int, window_seconds: int
) -> Callable[[Request], Awaitable[None]]:
    """Per-IP limit as a route dependency: ``dependencies=[Depends(rate_limit(...))]``."""

    async def dependency(request: Request) -> None:
        limiter = RateLimiter(request.app.state.redis)
        await limiter.hit(
            f"{scope}:ip:{client_ip(request)}", limit=limit, window_seconds=window_seconds
        )

    return dependency
