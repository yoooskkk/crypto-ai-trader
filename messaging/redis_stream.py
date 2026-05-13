"""
Redis Stream 封装
生产者/消费者解耦，支持消费者组
"""
import json
import os
from typing import AsyncIterator

import redis.asyncio as aioredis


def _get_client() -> aioredis.Redis:
    return aioredis.from_url(
        f"redis://{os.getenv('REDIS_HOST', 'localhost')}:{os.getenv('REDIS_PORT', 6379)}"
    )


class StreamProducer:
    def __init__(self):
        self._r = _get_client()

    async def publish(self, stream: str, data: dict) -> None:
        await self._r.xadd(stream, {"payload": json.dumps(data)}, maxlen=10_000)


class StreamConsumer:
    def __init__(self, group: str, consumer: str):
        self._r = _get_client()
        self.group = group
        self.consumer = consumer

    async def subscribe(self, stream: str) -> AsyncIterator[dict]:
        try:
            await self._r.xgroup_create(stream, self.group, id="0", mkstream=True)
        except Exception:
            pass
        while True:
            msgs = await self._r.xreadgroup(
                self.group, self.consumer, {stream: ">"}, count=10, block=1000
            )
            for _, entries in (msgs or []):
                for msg_id, fields in entries:
                    yield json.loads(fields[b"payload"])
                    await self._r.xack(stream, self.group, msg_id)
