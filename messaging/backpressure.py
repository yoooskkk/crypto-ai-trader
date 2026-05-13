"""
背压控制：当 Redis Stream 堆积超过阈值时，暂停生产者
"""
import asyncio
import logging

logger = logging.getLogger(__name__)

MAX_PENDING = 5_000


async def check_backpressure(redis_client, stream: str) -> None:
    info = await redis_client.xinfo_stream(stream)
    pending = info.get("length", 0)
    if pending > MAX_PENDING:
        logger.warning("Backpressure: %s pending=%d, sleeping 2s", stream, pending)
        await asyncio.sleep(2)
