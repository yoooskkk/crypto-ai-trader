"""
LLM 客户端
- 支持 OpenAI / DeepSeek / Anthropic 后端
- 超时 + 重试 + 降级策略
- 所有调用记录到 decision_logger

后端选择（按优先级）:
  1. LLMClient(backend="deepseek") 构造函数参数
  2. LLM_BACKEND 环境变量 (openai / deepseek / anthropic)
  3. 默认 "openai"

DeepSeek 使用 OpenAI 兼容 API，但 base_url 不同:
  - 基础 URL: https://api.deepseek.com
  - 模型: deepseek-chat
  - API Key 格式: sk-xxxxxxxx
"""
import asyncio
import os
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)

TIMEOUT = 30
MAX_RETRIES = 3

# ─── Docker Secrets 回退读取 ─────────────────────────────
# Docker Compose secrets 以文件形式挂载在 /run/secrets/<name>
# 当环境变量不存在时，尝试读取对应的 Docker secret 文件
SECRETS_DIR = "/run/secrets"


def _get_api_key(env_var: str, secret_name: str | None = None) -> str | None:
    """
    获取 API Key：优先从环境变量读取，失败则回退到 Docker secret 文件。

    参数:
        env_var: 环境变量名（如 "OPENAI_API_KEY"）
        secret_name: Docker secret 文件名（如 "llm_api_key"），默认取 env_var 小写

    返回:
        API Key 字符串，或 None（均未找到）
    """
    # 1. 环境变量
    key = os.getenv(env_var)
    if key:
        return key

    # 2. Docker secret 文件回退
    secret_file = secret_name or env_var.lower().replace("_", "_")
    secret_path = os.path.join(SECRETS_DIR, secret_file)
    try:
        with open(secret_path) as f:
            key = f.read().strip()
        if key:
            logger.debug("从 Docker secret 文件读取 API Key", path=secret_path)
            return key
    except (FileNotFoundError, PermissionError, IsADirectoryError):
        pass

    return None

# ─── 后端配置 ────────────────────────────────────────────────

BACKEND_CONFIG = {
    "openai": {
        "api_key_env": "OPENAI_API_KEY",
        "base_url": None,  # 使用 OpenAI 默认
        "default_model": "gpt-4o",
    },
    "deepseek": {
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
    },
    "anthropic": {
        "api_key_env": "ANTHROPIC_API_KEY",
        "base_url": None,
        "default_model": "claude-3-5-sonnet-20241022",
    },
}


class LLMClient:
    def __init__(self, backend: str | None = None):
        """
        初始化 LLM 客户端。

        参数:
            backend: 后端名称。None = 从 LLM_BACKEND 环境变量读取，默认 "openai"。
        """
        self.backend = backend or os.getenv("LLM_BACKEND", "openai").lower()

        if self.backend not in BACKEND_CONFIG:
            raise ValueError(
                f"未知后端: {self.backend}。可选: {', '.join(BACKEND_CONFIG.keys())}"
            )

        self._config = BACKEND_CONFIG[self.backend]

    async def complete(self, prompt: str, system: str = "") -> Optional[str]:
        for attempt in range(MAX_RETRIES):
            try:
                return await asyncio.wait_for(
                    self._call(prompt, system), timeout=TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "LLM 超时",
                    backend=self.backend,
                    attempt=attempt + 1,
                    max_retries=MAX_RETRIES,
                )
            except Exception as exc:
                logger.error("LLM 错误", backend=self.backend, error=str(exc))
            await asyncio.sleep(2 ** attempt)

        logger.error(
            "LLM 重试耗尽，触发 fallback",
            backend=self.backend,
            retries=MAX_RETRIES,
        )
        return None  # 调用方应触发 fallback_handler

    async def _call(self, prompt: str, system: str) -> str:
        if self.backend == "openai":
            return await self._call_openai(prompt, system)
        elif self.backend == "deepseek":
            return await self._call_openai_compat(prompt, system)
        elif self.backend == "anthropic":
            return await self._call_anthropic(prompt, system)
        raise ValueError(f"未知后端: {self.backend}")

    async def _call_openai(self, prompt: str, system: str) -> str:
        """OpenAI 原生 API。"""
        from openai import AsyncOpenAI

        api_key = _get_api_key(
            self._config["api_key_env"],
            secret_name="llm_api_key",
        )
        if not api_key:
            raise ValueError(
                f"API Key 未配置：请设置 {self._config['api_key_env']} 环境变量，"
                f"或创建 Docker secret llm_api_key"
            )
        client = AsyncOpenAI(api_key=api_key)
        resp = await client.chat.completions.create(
            model=self._config["default_model"],
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": prompt},
            ],
        )
        return resp.choices[0].message.content

    async def _call_openai_compat(self, prompt: str, system: str) -> str:
        """
        OpenAI 兼容 API（如 DeepSeek、Groq、Together AI 等）。

        通过以下环境变量自定义:
          LLM_API_BASE   — 覆盖 base_url（默认取 BACKEND_CONFIG）
          LLM_MODEL      — 覆盖 model（默认取 BACKEND_CONFIG）
          LLM_API_KEY    — 覆盖 API Key（默认取对应 env var）
        """
        from openai import AsyncOpenAI

        base_url = os.getenv("LLM_API_BASE") or self._config["base_url"]
        model = os.getenv("LLM_MODEL") or self._config["default_model"]
        api_key = (
            os.getenv("LLM_API_KEY")
            or _get_api_key(self._config["api_key_env"])
        )
        if not api_key:
            raise ValueError(
                f"API Key 未配置：请设置 {self._config['api_key_env']} 环境变量，"
                f"或创建 Docker secret llm_api_key"
            )

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": prompt},
            ],
        )
        return resp.choices[0].message.content

    async def _call_anthropic(self, prompt: str, system: str) -> str:
        """Anthropic API。"""
        from anthropic import AsyncAnthropic

        api_key = _get_api_key(self._config["api_key_env"])
        if not api_key:
            raise ValueError(
                f"API Key 未配置：请设置 {self._config['api_key_env']} 环境变量"
            )
        client = AsyncAnthropic(api_key=api_key)
        resp = await client.messages.create(
            model=self._config["default_model"],
            max_tokens=2048,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text
