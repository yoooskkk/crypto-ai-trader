#!/usr/bin/env python3
"""
crypto-ai-trader — 完整项目目录自动生成脚本
运行: python setup_project.py [目标目录]  默认在当前目录创建 crypto-ai-trader/
"""

import os
import sys
import textwrap
from pathlib import Path


# ──────────────────────────────────────────────
# 目录树定义
# 格式: (相对路径, 是否为文件, 文件内容模板key 或 None)
# ──────────────────────────────────────────────
STRUCTURE = [
    # ── 根文件
    ("docker-compose.yml",                   True,  "docker_compose"),
    (".env.example",                          True,  "env_example"),
    (".gitignore",                            True,  "gitignore"),
    ("README.md",                             True,  "readme"),
    ("requirements.txt",                      True,  "requirements"),
    ("pyproject.toml",                        True,  "pyproject"),

    # ── data/ 数据采集层
    ("data/__init__.py",                      True,  "init"),
    ("data/ws_client.py",                     True,  "ws_client"),
    ("data/rest_client.py",                   True,  "rest_client"),
    ("data/reconnect_guard.py",               True,  "reconnect_guard"),
    ("data/data_validator.py",                True,  "data_validator"),
    ("data/gap_filler.py",                    True,  "gap_filler"),
    ("data/market_selector.py",               True,  "market_selector"),
    ("data/news_scraper.py",                  True,  "news_scraper"),
    ("data/sentiment_feed.py",                True,  "sentiment_feed"),

    # ── messaging/ 消息队列层
    ("messaging/__init__.py",                 True,  "init"),
    ("messaging/redis_stream.py",             True,  "redis_stream"),
    ("messaging/producer.py",                 True,  "producer"),
    ("messaging/consumer.py",                 True,  "consumer"),
    ("messaging/backpressure.py",             True,  "backpressure"),

    # ── indicators/ 指标计算层
    ("indicators/__init__.py",                True,  "init"),
    ("indicators/trend.py",                   True,  "stub"),
    ("indicators/momentum.py",                True,  "stub"),
    ("indicators/volatility.py",              True,  "stub"),
    ("indicators/volume.py",                  True,  "stub"),
    ("indicators/timeseries.py",              True,  "stub"),
    ("indicators/math_factors.py",            True,  "stub"),
    ("indicators/crypto_alpha.py",            True,  "stub"),
    ("indicators/indicator_display.py",       True,  "stub"),
    ("indicators/cache_manager.py",           True,  "stub"),

    # ── regime/ 市场制度识别层
    ("regime/__init__.py",                    True,  "init"),
    ("regime/detector.py",                    True,  "regime_detector"),
    ("regime/hmm_model.py",                   True,  "stub"),
    ("regime/strategy_switcher.py",           True,  "stub"),
    ("regime/models/.gitkeep",                True,  "empty"),

    # ── analysis/ 分析层
    ("analysis/__init__.py",                  True,  "init"),
    ("analysis/multi_tf_trend.py",            True,  "stub"),
    ("analysis/factor_mining.py",             True,  "stub"),
    ("analysis/prompt_builder.py",            True,  "stub"),
    ("analysis/news_integrator.py",           True,  "stub"),
    ("analysis/pnl_attribution.py",           True,  "stub"),

    # ── ai_engine/ AI引擎层
    ("ai_engine/__init__.py",                 True,  "init"),
    ("ai_engine/llm_client.py",               True,  "llm_client"),
    ("ai_engine/schema_validator.py",         True,  "schema_validator"),
    ("ai_engine/signal_scorer.py",            True,  "stub"),
    ("ai_engine/plan_generator.py",           True,  "stub"),
    ("ai_engine/strategy_adapter.py",         True,  "stub"),
    ("ai_engine/prompt_versioner.py",         True,  "prompt_versioner"),
    ("ai_engine/fallback_handler.py",         True,  "stub"),

    # ── risk_guardian/ 风险控制层 ★核心新增
    ("risk_guardian/__init__.py",             True,  "init"),
    ("risk_guardian/circuit_breaker.py",      True,  "circuit_breaker"),
    ("risk_guardian/exposure_monitor.py",     True,  "stub"),
    ("risk_guardian/drawdown_limit.py",       True,  "stub"),
    ("risk_guardian/signal_arbiter.py",       True,  "stub"),
    ("risk_guardian/position_sizer.py",       True,  "stub"),

    # ── validation/ 回测验证层 ★核心新增
    ("validation/__init__.py",                True,  "init"),
    ("validation/output_schema.py",           True,  "output_schema"),
    ("validation/walk_forward.py",            True,  "stub"),
    ("validation/oos_test.py",                True,  "stub"),
    ("validation/factor_decay.py",            True,  "stub"),
    ("validation/paper_trading_parallel.py",  True,  "stub"),
    ("validation/datasets/train/.gitkeep",    True,  "empty"),
    ("validation/datasets/validate/.gitkeep", True,  "empty"),
    ("validation/datasets/oos/.gitkeep",      True,  "empty"),

    # ── freqtrade_strategies/ 策略执行层
    ("freqtrade_strategies/__init__.py",               True,  "init"),
    ("freqtrade_strategies/AiSignalStrategy.py",       True,  "ai_strategy"),
    ("freqtrade_strategies/config.json",               True,  "ft_config"),
    ("freqtrade_strategies/user_data/data/.gitkeep",   True,  "empty"),
    ("freqtrade_strategies/user_data/backtest_results/.gitkeep", True, "empty"),
    ("freqtrade_strategies/user_data/logs/.gitkeep",   True,  "empty"),

    # ── observability/ 可观测性层 ★核心新增
    ("observability/__init__.py",             True,  "init"),
    ("observability/decision_logger.py",      True,  "decision_logger"),
    ("observability/factor_decay_monitor.py", True,  "stub"),
    ("observability/alert_manager.py",        True,  "stub"),
    ("observability/prometheus/rules.yml",    True,  "empty"),
    ("observability/grafana/dashboards/.gitkeep", True, "empty"),

    # ── security/ 安全层 ★核心新增
    ("security/__init__.py",                  True,  "init"),
    ("security/secrets_loader.py",            True,  "secrets_loader"),
    ("security/audit_logger.py",              True,  "stub"),
    ("security/api_key_rotator.py",           True,  "stub"),

    # ── messaging infra
    ("infra/redis/redis.conf",                True,  "redis_conf"),
    ("infra/timescaledb/init.sql",            True,  "timescale_sql"),
    ("infra/influxdb/.gitkeep",               True,  "empty"),
    ("infra/prometheus/prometheus.yml",       True,  "prom_yml"),

    # ── ui/
    ("ui/__init__.py",                        True,  "init"),
    ("ui/cli/coin_selector.py",               True,  "stub"),
    ("ui/cli/timeframe_picker.py",            True,  "stub"),
    ("ui/cli/indicator_panel.py",             True,  "stub"),
    ("ui/dashboard/app.py",                   True,  "stub"),
    ("ui/dashboard/templates/.gitkeep",       True,  "empty"),

    # ── config/
    ("config/indicators.yml",                 True,  "indicators_yml"),
    ("config/timeframes.yml",                 True,  "timeframes_yml"),
    ("config/risk.yml",                       True,  "risk_yml"),
    ("config/llm_prompts/market_analysis.j2", True,  "prompt_market"),
    ("config/llm_prompts/trade_plan.j2",      True,  "prompt_trade"),
    ("config/llm_prompts/versions.json",      True,  "prompt_versions"),

    # ── tests/
    ("tests/__init__.py",                     True,  "init"),
    ("tests/test_data_validator.py",          True,  "stub"),
    ("tests/test_indicators.py",              True,  "stub"),
    ("tests/test_schema_validator.py",        True,  "stub"),
    ("tests/test_circuit_breaker.py",         True,  "stub"),
    ("tests/test_risk_guardian.py",           True,  "stub"),
    ("tests/test_prompt_builder.py",          True,  "stub"),
    ("tests/test_factor_mining.py",           True,  "stub"),
    ("tests/conftest.py",                     True,  "stub"),

    # ── scripts/
    ("scripts/setup.sh",                      True,  "setup_sh"),
    ("scripts/backfill_data.py",              True,  "stub"),
    ("scripts/run_backtest.sh",               True,  "empty"),
    ("scripts/health_check.py",               True,  "stub"),
]


# ──────────────────────────────────────────────
# 文件内容模板
# ──────────────────────────────────────────────
def json_ft_config() -> str:
    import json
    cfg = {
        "max_open_trades": 5,
        "stake_currency": "USDT",
        "stake_amount": "unlimited",
        "tradable_balance_ratio": 0.8,
        "dry_run": True,
        "dry_run_wallet": 10000,
        "cancel_open_orders_on_exit": True,
        "trading_mode": "spot",
        "exchange": {
            "name": "binance",
            "key": "",
            "secret": "",
            "ccxt_config": {"enableRateLimit": True},
        },
        "telegram": {"enabled": False, "token": "", "chat_id": ""},
        "api_server": {
            "enabled": True,
            "listen_ip_address": "0.0.0.0",
            "listen_port": 8080,
            "verbosity": "error",
            "enable_openapi": False,
            "jwt_secret_key": "CHANGE_ME",
            "CORS_origins": [],
        },
        "bot_name": "crypto-ai-trader",
        "initial_state": "running",
        "force_entry_enable": False,
        "internals": {"process_throttle_secs": 5},
    }
    return json.dumps(cfg, indent=2)


TEMPLATES = {

"init": '"""Package init."""\n',

"empty": "",

"stub": textwrap.dedent("""\
    \"\"\"
    TODO: 实现此模块
    \"\"\"
    """),

"readme": textwrap.dedent("""\
    # crypto-ai-trader

    基于 Binance 数据 + 多指标体系 + AI引擎 + Freqtrade 的量化交易系统。

    ## 快速开始
    ```bash
    cp .env.example .env          # 填写 API 密钥
    bash scripts/setup.sh         # 初始化环境
    docker compose up -d          # 启动所有服务
    ```

    ## 系统架构
    见 docs/architecture.svg

    ## 层级说明
    | 层级 | 目录 | 职责 |
    |------|------|------|
    | 数据采集 | data/ | WS+REST+断连重连+数据校验 |
    | 消息队列 | messaging/ | Redis Stream 解耦各服务 |
    | 指标计算 | indicators/ | 40+ 技术指标 + 币圈因子 |
    | 制度识别 | regime/ | HMM 市场状态分类 |
    | 分析层   | analysis/ | 多周期趋势 + 因子挖掘 |
    | AI 引擎  | ai_engine/ | LLM 交易计划 + Schema 校验 |
    | 风险控制 | risk_guardian/ | 熔断 + 暴露度 + 仲裁 |
    | 回测验证 | validation/ | Walk-Forward + OOS |
    | 策略执行 | freqtrade_strategies/ | Freqtrade 实盘 |
    | 可观测性 | observability/ | 决策链路日志 + 告警 |
    | 安全层   | security/ | 密钥管理 + 审计 |
    """),

"requirements": textwrap.dedent("""\
    # 数据采集
    websockets>=12.0
    aiohttp>=3.9
    python-binance>=1.0.19

    # 指标计算
    pandas>=2.2
    numpy>=1.26
    pandas-ta>=0.3.14b
    ta-lib>=0.4.28

    # AI 引擎
    openai>=1.30
    anthropic>=0.28
    pydantic>=2.7
    jinja2>=3.1

    # 消息队列 / 存储
    redis>=5.0
    psycopg2-binary>=2.9
    influxdb-client>=1.43

    # 市场制度识别
    hmmlearn>=0.3
    scikit-learn>=1.4

    # 回测 / 统计
    statsmodels>=0.14
    scipy>=1.13

    # 可观测性
    prometheus-client>=0.20
    structlog>=24.1

    # 工具
    python-dotenv>=1.0
    pyyaml>=6.0
    click>=8.1
    rich>=13.7
    """),

"pyproject": textwrap.dedent("""\
    [build-system]
    requires = ["setuptools>=68"]
    build-backend = "setuptools.backends.legacy:build"

    [project]
    name = "crypto-ai-trader"
    version = "0.1.0"
    requires-python = ">=3.11"

    [tool.pytest.ini_options]
    testpaths = ["tests"]
    asyncio_mode = "auto"

    [tool.ruff]
    line-length = 100
    """),

"docker_compose": textwrap.dedent("""\
    version: "3.9"

    services:

      redis:
        image: redis:7-alpine
        restart: unless-stopped
        volumes:
          - ./infra/redis/redis.conf:/usr/local/etc/redis/redis.conf
          - redis_data:/data
        command: redis-server /usr/local/etc/redis/redis.conf
        networks: [internal]

      timescaledb:
        image: timescale/timescaledb:latest-pg16
        restart: unless-stopped
        environment:
          POSTGRES_PASSWORD_FILE: /run/secrets/db_password
          POSTGRES_DB: trading
        secrets: [db_password]
        volumes:
          - ./infra/timescaledb/init.sql:/docker-entrypoint-initdb.d/init.sql
          - tsdb_data:/var/lib/postgresql/data
        networks: [internal]

      influxdb:
        image: influxdb:2.7-alpine
        restart: unless-stopped
        volumes:
          - influx_data:/var/lib/influxdb2
        networks: [internal]

      data-collector:
        build: .
        command: python -m data.ws_client
        restart: unless-stopped
        env_file: .env
        depends_on: [redis]
        networks: [internal, external]

      indicator-worker:
        build: .
        command: python -m messaging.consumer --group indicators
        restart: unless-stopped
        env_file: .env
        depends_on: [redis, timescaledb]
        networks: [internal]

      regime-worker:
        build: .
        command: python -m messaging.consumer --group regime
        restart: unless-stopped
        env_file: .env
        depends_on: [redis]
        networks: [internal]

      ai-engine:
        build: .
        command: python -m messaging.consumer --group ai_engine
        restart: unless-stopped
        env_file: .env
        secrets: [llm_api_key, binance_api_key]
        depends_on: [redis]
        networks: [internal]

      risk-guardian:
        build: .
        command: python -m risk_guardian.circuit_breaker
        restart: unless-stopped
        env_file: .env
        depends_on: [redis, ai-engine]
        networks: [internal]

      freqtrade:
        image: freqtradeorg/freqtrade:stable
        restart: unless-stopped
        volumes:
          - ./freqtrade_strategies:/freqtrade/user_data/strategies
          - ./freqtrade_strategies/user_data:/freqtrade/user_data
          - ./freqtrade_strategies/config.json:/freqtrade/config.json
        secrets: [binance_api_key, binance_api_secret]
        depends_on: [risk-guardian]
        networks: [internal, external]

      prometheus:
        image: prom/prometheus:latest
        restart: unless-stopped
        volumes:
          - ./infra/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml
          - ./observability/prometheus/rules.yml:/etc/prometheus/rules.yml
          - prom_data:/prometheus
        networks: [internal]

      grafana:
        image: grafana/grafana:latest
        restart: unless-stopped
        volumes:
          - ./observability/grafana/dashboards:/var/lib/grafana/dashboards
          - grafana_data:/var/lib/grafana
        networks: [internal]

    secrets:
      db_password:
        file: ./secrets/db_password.txt
      llm_api_key:
        file: ./secrets/llm_api_key.txt
      binance_api_key:
        file: ./secrets/binance_api_key.txt
      binance_api_secret:
        file: ./secrets/binance_api_secret.txt

    networks:
      internal:
        internal: true
      external: {}

    volumes:
      redis_data:
      tsdb_data:
      influx_data:
      prom_data:
      grafana_data:
    """),

"env_example": textwrap.dedent("""\
    # !! 复制为 .env 后填入真实值，.env 不得提交到版本控制 !!

    # Binance（建议只开 SPOT_TRADING 权限，关闭提现）
    BINANCE_API_KEY=your_key_here
    BINANCE_API_SECRET=your_secret_here
    BINANCE_TESTNET=false

    # LLM
    OPENAI_API_KEY=
    ANTHROPIC_API_KEY=

    # 数据库
    POSTGRES_HOST=timescaledb
    POSTGRES_PORT=5432
    POSTGRES_DB=trading
    POSTGRES_USER=trader

    # Redis
    REDIS_HOST=redis
    REDIS_PORT=6379

    # InfluxDB
    INFLUX_URL=http://influxdb:8086
    INFLUX_TOKEN=
    INFLUX_ORG=trading
    INFLUX_BUCKET=indicators

    # 风控参数（可在 config/risk.yml 覆盖）
    MAX_DAILY_DRAWDOWN_PCT=5.0
    MAX_EXPOSURE_PCT=80.0
    CIRCUIT_BREAKER_ENABLED=true

    # 环境
    ENV=production
    LOG_LEVEL=INFO
    """),

"gitignore": textwrap.dedent("""\
    .env
    secrets/
    *.pyc
    __pycache__/
    .pytest_cache/
    .ruff_cache/
    *.egg-info/
    dist/
    build/
    freqtrade_strategies/user_data/data/
    freqtrade_strategies/user_data/logs/
    freqtrade_strategies/user_data/backtest_results/
    validation/datasets/oos/
    regime/models/*.pkl
    *.log
    .DS_Store
    """),

"ws_client": textwrap.dedent("""\
    \"\"\"
    Binance WebSocket 客户端
    - 订阅 K线/深度/归集成交
    - 自动心跳 + 断连重连（委托 reconnect_guard）
    - 数据写入 Redis Stream
    \"\"\"
    import asyncio
    import json
    import logging
    from typing import Callable

    import websockets

    from messaging.producer import StreamProducer
    from data.reconnect_guard import ReconnectGuard

    logger = logging.getLogger(__name__)


    class BinanceWSClient:
        BASE_URL = "wss://stream.binance.com:9443/ws"

        def __init__(self, symbols: list[str], interval: str = "1m"):
            self.symbols = symbols
            self.interval = interval
            self.producer = StreamProducer()
            self._guard = ReconnectGuard(max_retries=20, base_delay=1.0)

        async def run(self) -> None:
            streams = "/".join(
                f"{s.lower()}@kline_{self.interval}" for s in self.symbols
            )
            url = f"{self.BASE_URL}/{streams}"
            async for attempt in self._guard:
                try:
                    async with websockets.connect(url, ping_interval=20) as ws:
                        logger.info("WS connected: %s streams", len(self.symbols))
                        self._guard.reset()
                        async for raw in ws:
                            msg = json.loads(raw)
                            await self.producer.publish("raw_kline", msg)
                except Exception as exc:
                    logger.warning("WS error: %s", exc)
                    await attempt.sleep()
    """),

"reconnect_guard": textwrap.dedent("""\
    \"\"\"
    断连重连守卫：指数退避 + 最大重试次数
    \"\"\"
    import asyncio
    import logging
    import math

    logger = logging.getLogger(__name__)


    class _Attempt:
        def __init__(self, n: int, base: float) -> None:
            self._delay = min(base * (2 ** n), 60.0)

        async def sleep(self) -> None:
            logger.info("Reconnecting in %.1fs", self._delay)
            await asyncio.sleep(self._delay)


    class ReconnectGuard:
        def __init__(self, max_retries: int = 20, base_delay: float = 1.0):
            self._max = max_retries
            self._base = base_delay
            self._n = 0

        def reset(self) -> None:
            self._n = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._n >= self._max:
                raise StopAsyncIteration
            attempt = _Attempt(self._n, self._base)
            self._n += 1
            return attempt
    """),

"data_validator": textwrap.dedent("""\
    \"\"\"
    数据异常检测
    - 价格跳空检测（单根K线涨跌超阈值）
    - 成交量异常（超过N倍滚动均值）
    - 时间戳连续性检测
    \"\"\"
    from dataclasses import dataclass
    from typing import Optional
    import numpy as np


    @dataclass
    class ValidationResult:
        valid: bool
        reason: Optional[str] = None


    class KlineValidator:
        def __init__(
            self,
            max_price_change_pct: float = 15.0,
            volume_spike_multiplier: float = 20.0,
        ):
            self.max_price_change = max_price_change_pct / 100
            self.vol_spike = volume_spike_multiplier
            self._vol_history: list[float] = []

        def validate(self, kline: dict) -> ValidationResult:
            o, h, l, c = (
                float(kline["o"]), float(kline["h"]),
                float(kline["l"]), float(kline["c"]),
            )
            # 价格跳空
            change = abs(c - o) / max(o, 1e-10)
            if change > self.max_price_change:
                return ValidationResult(False, f"Price spike {change:.1%}")
            # HL 逻辑
            if h < l or h < max(o, c) or l > min(o, c):
                return ValidationResult(False, "OHLC logic error")
            # 成交量
            vol = float(kline.get("v", 0))
            if self._vol_history:
                avg = np.mean(self._vol_history[-50:])
                if vol > avg * self.vol_spike:
                    return ValidationResult(False, f"Volume spike {vol/avg:.0f}x")
            self._vol_history.append(vol)
            return ValidationResult(True)
    """),

"gap_filler": textwrap.dedent("""\
    \"\"\"
    数据缺口补全
    WS 断连恢复后，用 REST API 补全缺失的 K 线
    \"\"\"
    import logging
    from datetime import datetime, timezone

    logger = logging.getLogger(__name__)


    class GapFiller:
        def __init__(self, rest_client):
            self.rest = rest_client
            self._last_ts: dict[str, int] = {}

        async def fill(self, symbol: str, interval: str, current_ts: int) -> list[dict]:
            last = self._last_ts.get(f"{symbol}_{interval}")
            if last is None:
                self._last_ts[f"{symbol}_{interval}"] = current_ts
                return []
            gap_ms = current_ts - last
            interval_ms = self._interval_to_ms(interval)
            if gap_ms <= interval_ms * 1.5:
                self._last_ts[f"{symbol}_{interval}"] = current_ts
                return []
            logger.warning("Gap detected %s %s: %dms", symbol, interval, gap_ms)
            klines = await self.rest.get_klines(symbol, interval, start=last, end=current_ts)
            self._last_ts[f"{symbol}_{interval}"] = current_ts
            return klines

        @staticmethod
        def _interval_to_ms(interval: str) -> int:
            units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
            return int(interval[:-1]) * units[interval[-1]]
    """),

"redis_stream": textwrap.dedent("""\
    \"\"\"
    Redis Stream 封装
    生产者/消费者解耦，支持消费者组
    \"\"\"
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
    """),

"producer": '"""生产者便捷封装"""\nfrom messaging.redis_stream import StreamProducer\n__all__ = ["StreamProducer"]\n',

"consumer": '"""消费者便捷封装"""\nfrom messaging.redis_stream import StreamConsumer\n__all__ = ["StreamConsumer"]\n',

"backpressure": textwrap.dedent("""\
    \"\"\"
    背压控制：当 Redis Stream 堆积超过阈值时，暂停生产者
    \"\"\"
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
    """),

"regime_detector": textwrap.dedent("""\
    \"\"\"
    市场制度识别
    使用 ADX + BollingerBand 宽度的规则方法（快速）
    或 HMM 模型（精确，需训练）
    \"\"\"
    from enum import Enum
    from dataclasses import dataclass
    import numpy as np


    class Regime(str, Enum):
        TRENDING   = "trending"
        RANGING    = "ranging"
        HIGH_VOLAT = "high_volatility"
        UNKNOWN    = "unknown"


    @dataclass
    class RegimeResult:
        regime: Regime
        confidence: float
        adx: float
        bb_width: float


    class RuleBasedDetector:
        \"\"\"
        规则:
          ADX > 25 且 BB宽度适中 → 趋势
          ADX < 20 且 BB宽度窄   → 震荡
          BB宽度极大             → 高波动
        \"\"\"
        def __init__(self, adx_trend=25.0, adx_range=20.0, bb_wide=0.08, bb_narrow=0.02):
            self.adx_trend  = adx_trend
            self.adx_range  = adx_range
            self.bb_wide    = bb_wide
            self.bb_narrow  = bb_narrow

        def detect(self, adx: float, bb_width: float) -> RegimeResult:
            if bb_width > self.bb_wide:
                return RegimeResult(Regime.HIGH_VOLAT, 0.85, adx, bb_width)
            if adx > self.adx_trend:
                return RegimeResult(Regime.TRENDING, min(adx / 50, 1.0), adx, bb_width)
            if adx < self.adx_range and bb_width < self.bb_narrow:
                return RegimeResult(Regime.RANGING, 0.75, adx, bb_width)
            return RegimeResult(Regime.UNKNOWN, 0.4, adx, bb_width)
    """),

"llm_client": textwrap.dedent("""\
    \"\"\"
    LLM 客户端
    - 支持 OpenAI / Anthropic 双后端
    - 超时 + 重试 + 降级策略
    - 所有调用记录到 decision_logger
    \"\"\"
    import asyncio
    import logging
    import os
    from typing import Optional

    logger = logging.getLogger(__name__)

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
                    logger.warning("LLM timeout attempt %d/%d", attempt + 1, MAX_RETRIES)
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
    """),

"schema_validator": textwrap.dedent("""\
    \"\"\"
    AI 输出结构化校验
    用 Pydantic 强约束 LLM 返回的交易计划格式
    \"\"\"
    from enum import Enum
    from typing import Optional
    from pydantic import BaseModel, Field, field_validator


    class Direction(str, Enum):
        LONG  = "long"
        SHORT = "short"
        FLAT  = "flat"


    class TradePlan(BaseModel):
        symbol:      str
        direction:   Direction
        confidence:  float        = Field(ge=0.0, le=1.0)
        entry_price: Optional[float] = None
        stop_loss:   Optional[float] = None
        take_profit: Optional[float] = None
        reasoning:   str          = Field(min_length=20)
        regime:      str          = "unknown"
        timeframe:   str          = "1h"

        @field_validator("stop_loss")
        @classmethod
        def sl_must_be_rational(cls, v, info):
            if v and info.data.get("direction") == Direction.LONG:
                entry = info.data.get("entry_price")
                if entry and v >= entry:
                    raise ValueError("LONG stop_loss must be below entry_price")
            return v


    def parse_trade_plan(raw: str) -> Optional[TradePlan]:
        import json, re
        try:
            data = json.loads(re.sub(r"```json|```", "", raw).strip())
            return TradePlan(**data)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error("Schema validation failed: %s", exc)
            return None
    """),

"prompt_versioner": textwrap.dedent("""\
    \"\"\"
    Prompt 版本管理
    每次 LLM 调用记录使用的 Prompt 版本，确保决策可溯源
    \"\"\"
    import hashlib
    import json
    import logging
    from pathlib import Path

    logger = logging.getLogger(__name__)
    VERSION_FILE = Path("config/llm_prompts/versions.json")


    class PromptVersioner:
        def __init__(self):
            self._registry: dict = {}
            if VERSION_FILE.exists():
                self._registry = json.loads(VERSION_FILE.read_text())

        def register(self, name: str, template: str) -> str:
            version = hashlib.sha1(template.encode()).hexdigest()[:8]
            self._registry[name] = {"version": version, "hash": version}
            VERSION_FILE.write_text(json.dumps(self._registry, indent=2))
            return version

        def get_version(self, name: str) -> str:
            return self._registry.get(name, {}).get("version", "unknown")
    """),

"circuit_breaker": textwrap.dedent("""\
    \"\"\"
    熔断器 — 风险控制核心
    触发条件：
      1. 单日回撤超过 MAX_DAILY_DRAWDOWN_PCT
      2. 账户净值低于 EQUITY_FLOOR
      3. 连续亏损单数超过 MAX_CONSECUTIVE_LOSSES
    熔断后：拒绝所有新开仓信号，只允许平仓
    \"\"\"
    import logging
    import os
    from dataclasses import dataclass, field
    from datetime import date
    from enum import Enum

    logger = logging.getLogger(__name__)


    class BreakerState(str, Enum):
        CLOSED  = "closed"   # 正常
        OPEN    = "open"     # 熔断中
        HALF    = "half"     # 冷静期（仅允许小仓位）


    @dataclass
    class CircuitBreaker:
        max_daily_dd:   float = float(os.getenv("MAX_DAILY_DRAWDOWN_PCT", 5.0))
        equity_floor:   float = float(os.getenv("EQUITY_FLOOR_USD", 0.0))
        max_consec_loss: int  = int(os.getenv("MAX_CONSECUTIVE_LOSSES", 5))
        state:          BreakerState = field(default=BreakerState.CLOSED, init=False)
        _consec:        int          = field(default=0, init=False)
        _day_start_eq:  float        = field(default=0.0, init=False)
        _today:         date         = field(default_factory=date.today, init=False)

        def update_equity(self, current_equity: float) -> None:
            today = date.today()
            if today != self._today:
                self._day_start_eq = current_equity
                self._today = today
            if self._day_start_eq == 0:
                self._day_start_eq = current_equity
                return
            dd = (self._day_start_eq - current_equity) / self._day_start_eq * 100
            if dd >= self.max_daily_dd:
                self._trip(f"Daily drawdown {dd:.2f}% >= {self.max_daily_dd}%")
            if self.equity_floor and current_equity < self.equity_floor:
                self._trip(f"Equity {current_equity:.2f} below floor {self.equity_floor:.2f}")

        def record_trade(self, pnl: float) -> None:
            if pnl < 0:
                self._consec += 1
                if self._consec >= self.max_consec_loss:
                    self._trip(f"Consecutive losses: {self._consec}")
            else:
                self._consec = 0

        def allow_open(self) -> bool:
            return self.state == BreakerState.CLOSED

        def reset(self) -> None:
            self.state = BreakerState.CLOSED
            logger.info("Circuit breaker reset")

        def _trip(self, reason: str) -> None:
            if self.state != BreakerState.OPEN:
                self.state = BreakerState.OPEN
                logger.critical("CIRCUIT BREAKER OPEN: %s", reason)
    """),

"output_schema": textwrap.dedent("""\
    \"\"\"
    回测验证 Schema — 与 ai_engine/schema_validator.py 共享 TradePlan 模型
    另外定义回测结果的验证结构
    \"\"\"
    from pydantic import BaseModel, Field


    class BacktestResult(BaseModel):
        strategy:       str
        start_date:     str
        end_date:       str
        total_trades:   int   = Field(ge=0)
        win_rate:       float = Field(ge=0.0, le=1.0)
        sharpe:         float
        max_drawdown:   float = Field(le=0.0)
        profit_factor:  float = Field(ge=0.0)
        avg_trade_pct:  float


    class WalkForwardResult(BaseModel):
        windows: list[BacktestResult]
        avg_sharpe:      float
        sharpe_variance: float
        robust:          bool  # sharpe_variance < threshold
    """),

"decision_logger": textwrap.dedent("""\
    \"\"\"
    决策链路日志
    记录每一次 AI 决策的完整上下文：
    输入指标 + Prompt版本 + LLM原始输出 + 校验结果 + 最终信号
    存入 TimescaleDB 便于事后复盘
    \"\"\"
    import json
    import logging
    from datetime import datetime, timezone
    from dataclasses import dataclass, asdict
    from typing import Optional

    logger = logging.getLogger(__name__)


    @dataclass
    class DecisionRecord:
        ts:             str
        symbol:         str
        timeframe:      str
        prompt_version: str
        regime:         str
        raw_llm_output: str
        validated:      bool
        direction:      Optional[str]
        confidence:     Optional[float]
        breaker_state:  str
        signal_sent:    bool

    class DecisionLogger:
        def __init__(self, db_conn=None):
            self._db = db_conn

        def log(self, record: DecisionRecord) -> None:
            logger.info("DECISION %s", json.dumps(asdict(record)))
            if self._db:
                self._write_db(record)

        def _write_db(self, record: DecisionRecord) -> None:
            # TODO: INSERT INTO decision_log
            pass
    """),

"secrets_loader": textwrap.dedent("""\
    \"\"\"
    密钥加载器
    优先级: Docker Secrets > 环境变量 > .env
    绝不将密钥写入日志
    \"\"\"
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
            "anthropic": ("llm_api_key", "ANTHROPIC_API_KEY"),
        }
        name, env = mapping[backend]
        return load_secret(name, env)
    """),

"ai_strategy": textwrap.dedent("""\
    \"\"\"
    AiSignalStrategy — Freqtrade 主策略
    从 Redis 读取 AI 引擎生成的信号，经风险控制层仲裁后执行
    \"\"\"
    from freqtrade.strategy import IStrategy
    import pandas as pd


    class AiSignalStrategy(IStrategy):
        INTERFACE_VERSION = 3
        timeframe = "1h"
        minimal_roi = {"0": 0.05}
        stoploss = -0.03
        trailing_stop = True

        def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
            # 指标由独立 worker 预计算，此处直接读取缓存
            return dataframe

        def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
            # TODO: 从 Redis 读取 AI 信号
            dataframe["enter_long"]  = 0
            dataframe["enter_short"] = 0
            return dataframe

        def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
            dataframe["exit_long"]  = 0
            dataframe["exit_short"] = 0
            return dataframe
    """),

"ft_config": json_ft_config(),

"indicators_yml": textwrap.dedent("""\
    # 指标参数配置
    trend:
      ema_periods: [9, 21, 55, 200]
      sma_periods: [20, 50, 200]
      macd: {fast: 12, slow: 26, signal: 9}
      adx_period: 14

    momentum:
      rsi_period: 14
      roc_period: 10
      cci_period: 20
      stoch: {k: 14, d: 3, smooth_k: 3}

    volatility:
      atr_period: 14
      stddev_period: 20
      bbands: {period: 20, std: 2}

    volume:
      mfi_period: 14
      cmf_period: 20
      vol_ratio_period: 20

    crypto_alpha:
      funding_rate_source: binance
      oi_delta_period: 24   # 小时
      cvd_lookback: 100
    """),

"timeframes_yml": textwrap.dedent("""\
    # 支持的时间周期（Freqtrade 格式）
    available:
      - 1m
      - 5m
      - 15m
      - 1h
      - 4h
      - 1d
      - 1w

    default: 1h

    multi_tf_consensus:
      primary: 1h
      confirm: [4h, 1d]
      fast:    [5m, 15m]
    """),

"risk_yml": textwrap.dedent("""\
    # 风险参数配置
    circuit_breaker:
      max_daily_drawdown_pct: 5.0
      equity_floor_usd: 0
      max_consecutive_losses: 5
      cool_down_hours: 4

    exposure:
      max_total_pct: 80.0
      max_single_position_pct: 20.0
      max_correlated_pairs: 3

    signal:
      min_confidence: 0.65
      require_regime_match: true
      ai_override_freqtrade: false
    """),

"prompt_market": textwrap.dedent("""\
    {# Jinja2 模板 #}
    你是一位顶尖的加密货币量化交易员，拥有10年做市商经验。
    请基于以下多时间周期指标，给出专业的市场状态分析。

    ## 当前市场制度
    - 制度类型: {{ regime }}
    - 置信度: {{ regime_confidence }}

    ## 多周期趋势
    {% for tf, trend in trends.items() %}
    - {{ tf }}: {{ trend.direction }} (强度: {{ trend.strength }})
    {% endfor %}

    ## 核心指标（{{ symbol }} / {{ timeframe }}）
    {% for name, value in indicators.items() %}
    - {{ name }}: {{ value }}
    {% endfor %}

    ## 币圈增强因子
    - 资金费率: {{ funding_rate }}
    - OI变化(24h): {{ oi_delta }}
    - CVD delta: {{ cvd_delta }}

    请以 JSON 格式回答，包含以下字段：
    direction (long/short/flat), confidence (0-1),
    entry_price, stop_loss, take_profit, reasoning, regime, timeframe
    """),

"prompt_trade": textwrap.dedent("""\
    {# 交易计划 Prompt 模板 #}
    基于以下市场分析，给出完整的交易执行计划。

    市场分析摘要: {{ market_summary }}
    当前价格: {{ current_price }}
    新闻情绪: {{ news_sentiment }} ({{ news_score }})

    请给出：
    1. 入场时机与触发条件
    2. 仓位建议（占总资金比例）
    3. 止损设置依据
    4. 分批获利目标
    5. 风险提示

    以 JSON 格式输出。
    """),

"prompt_versions": '{"market_analysis": {}, "trade_plan": {}}\n',

"redis_conf": textwrap.dedent("""\
    maxmemory 512mb
    maxmemory-policy allkeys-lru
    save 900 1
    save 300 10
    appendonly yes
    """),

"timescale_sql": textwrap.dedent("""\
    CREATE EXTENSION IF NOT EXISTS timescaledb;

    CREATE TABLE IF NOT EXISTS klines (
        ts          TIMESTAMPTZ NOT NULL,
        symbol      TEXT NOT NULL,
        interval    TEXT NOT NULL,
        open        DOUBLE PRECISION,
        high        DOUBLE PRECISION,
        low         DOUBLE PRECISION,
        close       DOUBLE PRECISION,
        volume      DOUBLE PRECISION
    );
    SELECT create_hypertable('klines', 'ts', if_not_exists => TRUE);

    CREATE TABLE IF NOT EXISTS indicators (
        ts          TIMESTAMPTZ NOT NULL,
        symbol      TEXT NOT NULL,
        interval    TEXT NOT NULL,
        name        TEXT NOT NULL,
        value       DOUBLE PRECISION
    );
    SELECT create_hypertable('indicators', 'ts', if_not_exists => TRUE);

    CREATE TABLE IF NOT EXISTS decision_log (
        ts              TIMESTAMPTZ NOT NULL,
        symbol          TEXT,
        timeframe       TEXT,
        prompt_version  TEXT,
        regime          TEXT,
        validated       BOOLEAN,
        direction       TEXT,
        confidence      DOUBLE PRECISION,
        breaker_state   TEXT,
        signal_sent     BOOLEAN,
        raw_output      TEXT
    );
    SELECT create_hypertable('decision_log', 'ts', if_not_exists => TRUE);
    """),

"prom_yml": textwrap.dedent("""\
    global:
      scrape_interval: 15s

    scrape_configs:
      - job_name: ai-engine
        static_configs:
          - targets: ['ai-engine:8000']
      - job_name: risk-guardian
        static_configs:
          - targets: ['risk-guardian:8001']
      - job_name: data-collector
        static_configs:
          - targets: ['data-collector:8002']
    """),

"setup_sh": textwrap.dedent("""\
    #!/usr/bin/env bash
    set -euo pipefail

    echo "=== crypto-ai-trader setup ==="

    # 创建 secrets 目录（本地开发用，生产用 Docker Secrets）
    mkdir -p secrets
    for f in db_password llm_api_key binance_api_key binance_api_secret; do
        [ -f "secrets/$f.txt" ] || echo "PLACEHOLDER" > "secrets/$f.txt"
    done

    # 复制环境变量模板
    [ -f .env ] || cp .env.example .env
    echo "请编辑 .env 和 secrets/ 目录填入真实密钥"

    # 拉取镜像
    docker compose pull

    echo "=== 初始化完成，运行 docker compose up -d 启动系统 ==="
    """),
}


# ──────────────────────────────────────────────
# 文件生成引擎
# ──────────────────────────────────────────────

def create_structure(root: Path) -> None:
    created_files = 0
    created_dirs: set[str] = set()

    for rel_path, is_file, template_key in STRUCTURE:
        full_path = root / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)

        dir_str = str(full_path.parent.relative_to(root))
        if dir_str not in created_dirs:
            created_dirs.add(dir_str)

        if is_file:
            if full_path.exists():
                print(f"  [skip]  {rel_path}")
                continue
            content = TEMPLATES.get(template_key, "")
            full_path.write_text(content, encoding="utf-8")
            created_files += 1
            print(f"  [+]     {rel_path}")

    # scripts/setup.sh 需要可执行权限
    setup_sh = root / "scripts" / "setup.sh"
    if setup_sh.exists():
        setup_sh.chmod(0o755)

    print(f"\n完成: 创建 {created_files} 个文件，{len(created_dirs)} 个目录")
    print(f"项目根目录: {root.resolve()}")


def print_tree(root: Path, prefix: str = "", max_depth: int = 4, _depth: int = 0) -> None:
    if _depth > max_depth:
        return
    entries = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name))
    for i, entry in enumerate(entries):
        is_last = i == len(entries) - 1
        connector = "└── " if is_last else "├── "
        print(prefix + connector + entry.name)
        if entry.is_dir() and not entry.name.startswith("."):
            extension = "    " if is_last else "│   "
            print_tree(entry, prefix + extension, max_depth, _depth + 1)


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "crypto-ai-trader"
    root = Path(target)

    print(f"\n构建项目: {root.resolve()}\n")
    create_structure(root)

    print("\n目录树预览 (depth=4):\n")
    print(root.name + "/")
    print_tree(root, max_depth=4)
    print("\n下一步:")
    print(f"  cd {root}")
    print("  cp .env.example .env  # 填写密钥")
    print("  bash scripts/setup.sh")
    print("  docker compose up -d")


if __name__ == "__main__":
    main()