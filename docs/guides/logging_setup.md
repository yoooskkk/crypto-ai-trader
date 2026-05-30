# 日志配置说明

## 统一入口

```python
from logging_setup import setup_logging
setup_logging()  # 在应用入口最顶部调用
```

## 模块内使用

所有模块统一使用 structlog：

```python
import structlog
logger = structlog.get_logger(__name__)
logger.info("消息", key=value)  # 键值对参数
logger.warning("警告", error=str(e))
logger.error("错误", extra_field=xxx)
```

**禁止**：
- ❌ `print(...)` — 除非是 CLI 交互（如 input 提示、表格展示）
- ❌ `logging.getLogger(...)` — 除非是第三方库内部使用

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LOG_LEVEL` | `INFO` | 日志级别：DEBUG / INFO / WARNING / ERROR |

## 输出格式

- **终端（TTY）**：彩色带时间戳的人类可读格式
- **非终端 / 管道**：JSON 格式（便于日志收集系统）
- **生产环境**：设置 `json_format=True` 强制 JSON

## 已迁移模块（structlog）

`rest_client.py` · `ws_client.py` · `market_selector.py` · `news_scraper.py` · `sentiment_feed.py` · `logging_setup.py` (自身) · `momentum.py` · `volatility.py` · `volume.py` · `timeseries.py` · `math_factors.py` · `crypto_alpha.py` · `cache_manager.py` · `indicator_display.py` · `coin_selector.py` · `timeframe_picker.py`

## 待迁移模块（仍使用 logging）

`trend.py` · `reconnect_guard.py` · `gap_filler.py` · `circuit_breaker.py` · `llm_client.py` · `prompt_versioner.py` · `decision_logger.py` · `backpressure.py` · `redis_stream.py` · `data_validator.py`
