#!/usr/bin/env python3
"""临时脚本：写入 logging_setup.py"""
content = '''"""
统一日志引导模块。

用法:
    from logging_setup import setup_logging
    setup_logging()

效果:
    1. 配置 structlog 处理器链
    2. 开发环境彩色 / 生产环境 JSON
    3. 将旧版 logging.getLogger() 输出统一转发到 structlog
"""

from __future__ import annotations

import logging
import os
import sys

import structlog


def setup_logging(
    level: str | None = None,
    json_format: bool = False,
) -> None:
    """初始化全局日志配置"""
    log_level_name = (level or os.getenv("LOG_LEVEL", "INFO")).upper()

    # 1. 配置 structlog 处理器链
    shared: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if sys.stderr.isatty() and not json_format:
        processors = shared + [structlog.dev.ConsoleRenderer()]
    else:
        processors = shared + [structlog.processors.JSONRenderer()]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # 2. 将 structlog 路由到 stdlib logging
    # 这样旧版 logging.getLogger() 和新版 structlog.get_logger() 走同一输出通道
    structlog.stdlib.recreate_defaults(
        log_level=getattr(logging, log_level_name, logging.INFO)
    )

    # 3. 减少第三方库噪音
    for noisy in ("websockets", "aiohttp", "urllib3", "hmmlearn", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger = structlog.get_logger("logging_setup")
    logger.info("日志系统初始化完成", level=log_level_name, json_format=json_format)
'''

with open("logging_setup.py", "w", encoding="utf-8") as f:
    f.write(content)
print("logging_setup.py 写入完成")
