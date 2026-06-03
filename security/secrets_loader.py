"""
密钥加载器
优先级: Docker Secrets > 环境变量 > .env
绝不将密钥写入日志
"""
import os
from pathlib import Path


def load_secret(name: str, env_var: str) -> str:
    secret_file = Path(f"/run/secrets/{name}")
    if secret_file.exists():
        return secret_file.read_text().strip()
    val = os.getenv(env_var, "")
    if not val:
        raise RuntimeError(
            f"Secret '{name}' not found in Docker Secrets or env var '{env_var}'"
        )
    return val


def get_binance_key() -> tuple[str, str]:
    return (
        load_secret("binance_api_key",    "BINANCE_API_KEY"),
        load_secret("binance_api_secret", "BINANCE_API_SECRET"),
    )


def get_llm_key(backend: str = "openai") -> str:
    mapping = {
        "openai":    ("llm_api_key", "OPENAI_API_KEY"),
        "deepseek":  ("llm_api_key", "DEEPSEEK_API_KEY"),
        "anthropic": ("llm_api_key", "ANTHROPIC_API_KEY"),
    }
    if backend not in mapping:
        raise ValueError(f"未知 LLM 后端: {backend}。可选: {', '.join(mapping.keys())}")
    name, env = mapping[backend]
    return load_secret(name, env)
