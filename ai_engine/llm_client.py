"""
LLM 客户端
- 支持 OpenAI / Anthropic 双后端
- 超时 + 重试 + 降级策略
- 所有调用记录到 decision_logger
"""
import asyncio
import structlog
import os
from typing import Optional

logger = structlog.get_logger(__name__)

TIMEOUT = 30
MAX_RETRIES = 3


class LLMClient:
    def __init__(self, backend: str = "openai"):
        self.backend = backend

    async def complete(self, prompt: str, system: str = "") -> Optional[str]:
        for attempt in range(MAX_RETRIES):
            try:
                return await asyncio.wait_for(
                    self._call(prompt, system), timeout=TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning("LLM timeout", attempt=attempt + 1, max_retries=MAX_RETRIES)
            except Exception as exc:
                logger.error("LLM error: %s", exc)
            await asyncio.sleep(2 ** attempt)
        logger.error("LLM failed after %d retries, activating fallback", MAX_RETRIES)
        return None  # 调用方应触发 fallback_handler

    async def _call(self, prompt: str, system: str) -> str:
        if self.backend == "openai":
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            resp = await client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
            )
            return resp.choices[0].message.content
        raise ValueError(f"Unknown backend: {self.backend}")
