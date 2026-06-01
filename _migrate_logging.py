"""临时脚本: 将 6 个文件的 logging → structlog 迁移。"""
from __future__ import annotations

files_to_logging_calls = {
    'indicators/trend.py': [
        ('logger = logging.getLogger(__name__)', 'logger = structlog.get_logger(__name__)'),
        ('import logging', 'import structlog'),
    ],
    'data/reconnect_guard.py': [
        ('logger = logging.getLogger(__name__)', 'logger = structlog.get_logger(__name__)'),
        ('import logging', 'import structlog'),
        ('logger.info("Reconnecting in %.1fs", self._delay)',
         'logger.info("Reconnecting", delay=self._delay)'),
    ],
    'data/gap_filler.py': [
        ('logger = logging.getLogger(__name__)', 'logger = structlog.get_logger(__name__)'),
        ('import logging', 'import structlog'),
        ('logger.warning("Gap detected %s %s: %dms", symbol, interval, gap_ms)',
         'logger.warning("Gap detected", symbol=symbol, interval=interval, gap_ms=gap_ms)'),
    ],
    'risk_guardian/circuit_breaker.py': [
        ('logger = logging.getLogger(__name__)', 'logger = structlog.get_logger(__name__)'),
        ('import logging', 'import structlog'),
    ],
    'ai_engine/llm_client.py': [
        ('logger = logging.getLogger(__name__)', 'logger = structlog.get_logger(__name__)'),
        ('import logging', 'import structlog'),
        ('logger.warning("LLM timeout attempt %d/%d", attempt + 1, MAX_RETRIES)',
         'logger.warning("LLM timeout", attempt=attempt + 1, max_retries=MAX_RETRIES)'),
    ],
    'ai_engine/prompt_versioner.py': [
        ('logger = logging.getLogger(__name__)', 'logger = structlog.get_logger(__name__)'),
        ('import logging', 'import structlog'),
    ],
}

for fp, replacements in files_to_logging_calls.items():
    with open(fp, encoding='utf-8') as f:
        content = f.read()

    for old, new in replacements:
        if old in content:
            content = content.replace(old, new)
            print(f'{fp}: replaced ✅')
        else:
            print(f'{fp}: "{old[:50]}..." NOT FOUND ❌')

    with open(fp, 'w', encoding='utf-8') as f:
        f.write(content)

print()
print('All replacements done.')
