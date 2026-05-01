"""
Redis / in-memory store provider
=================================
If REDIS_URL is set, connects to a real Redis server.
Otherwise, uses fakeredis (in-process), so no external service is needed
for single-container deployments (e.g. Zeabur).

Both the async API (FastAPI) and the sync Worker thread share the same
underlying FakeServer instance, so all state (jobs, sessions, queues) is
visible to both sides within the same process.
"""
import os

REDIS_URL = os.environ.get("REDIS_URL", "").strip()

if REDIS_URL:
    import redis as _redis
    import redis.asyncio as _aioredis

    def get_sync_redis():
        return _redis.from_url(REDIS_URL, decode_responses=True)

    async def get_async_redis():
        return _aioredis.from_url(REDIS_URL, decode_responses=True)

else:
    import fakeredis
    import fakeredis.aioredis as _fake_aioredis

    # One shared server so API and worker see the same data
    _server = fakeredis.FakeServer()

    def get_sync_redis():
        return fakeredis.FakeRedis(server=_server, decode_responses=True)

    async def get_async_redis():
        return _fake_aioredis.FakeRedis(server=_server, decode_responses=True)
