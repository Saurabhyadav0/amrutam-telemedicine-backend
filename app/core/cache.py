"""Cache-aside on Redis with generation-based invalidation.

Keys embed a namespace generation (``doctors:v17:...``). Invalidating a namespace is a single
``INCR`` of its generation; stale keys are never read again and simply expire by TTL. This
avoids ``SCAN``/``DEL`` storms and race windows between "delete" and "re-populate".

Redis failures degrade to cache misses (fail open): the database stays the source of truth.
"""

from collections.abc import Awaitable, Callable

import structlog
from prometheus_client import Counter
from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = structlog.get_logger(__name__)

CACHE_REQUESTS = Counter(
    "cache_requests_total", "Cache lookups by namespace and result", ["namespace", "result"]
)


class Cache:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def generation(self, namespace: str) -> int:
        try:
            value = await self._redis.get(f"cachegen:{namespace}")
        except RedisError:
            return 0
        return int(value) if value else 0

    async def invalidate(self, namespace: str) -> None:
        try:
            await self._redis.incr(f"cachegen:{namespace}")
        except RedisError:
            # Entries still expire by TTL; log so a sustained outage is visible.
            logger.warning("cache_invalidate_failed", namespace=namespace)

    async def get_or_load[M: BaseModel](
        self,
        namespace: str,
        key: str,
        model: type[M],
        loader: Callable[[], Awaitable[M]],
        *,
        ttl_seconds: int,
    ) -> M:
        full_key = f"cache:{namespace}:v{await self.generation(namespace)}:{key}"
        try:
            cached = await self._redis.get(full_key)
        except RedisError:
            cached = None
        if cached is not None:
            CACHE_REQUESTS.labels(namespace, "hit").inc()
            return model.model_validate_json(cached)

        CACHE_REQUESTS.labels(namespace, "miss").inc()
        value = await loader()
        try:
            await self._redis.set(full_key, value.model_dump_json(), ex=ttl_seconds)
        except RedisError:
            logger.warning("cache_set_failed", namespace=namespace)
        return value
