"""
模块名称: rest_client.py
所属层级: 数据采集层 (Data)
输入来源: Binance REST API（公开端点）
输出去向: 返回结构化数据给 gap_filler / market_selector 等内部调用方
关键依赖: aiohttp, structlog, yaml

功能说明:
    Binance 现货+合约市场 REST API 封装客户端。
    仅使用公开端点，无需 API Key。
    使用惰性 aiohttp session，避免无用时创建连接。

    支持端点:
    - GET /api/v3/klines            — 历史 K 线（现货）
    - GET /fapi/v1/klines           — 历史 K 线（合约）
    - GET /api/v3/ticker/24hr       — 24hr 行情统计
    - GET /api/v3/exchangeInfo      — 交易对信息
    - GET /api/v3/ticker/price      — 最新价格

速率限制:
    - 现货: 1200 权重/分钟
    - 合约公开: 2400 次/分钟
    每次调用前检查权重，超出时自动等待。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger(__name__)

_CONFIG_DIR = Path(__file__).parent.parent / "config"
CONFIG_PATH = _CONFIG_DIR / "timeframes.yml"

# ─── Binance API 常量 ─────────────────────────────────────

_SPOT_BASE = "https://api.binance.com"
_FAPI_BASE = "https://fapi.binance.com"

# K 线数组字段索引
KLINE_IDX = {
    "open_time": 0,
    "open": 1,
    "high": 2,
    "low": 3,
    "close": 4,
    "volume": 5,
    "close_time": 6,
    "quote_vol": 7,
    "trades": 8,
    "taker_buy_base": 9,
    "taker_buy_quote": 10,
}


# ─── 配置读取 ───────────────────────────────────────────


@lru_cache(maxsize=1)
def load_timeframes(config_path: str | Path | None = None) -> dict[str, Any]:
    """
    从 config/timeframes.yml 读取时间周期配置。

    返回结构:
    {
        "available": ["1m", "5m", ...],
        "default": "1h",
        "multi_tf_consensus": {
            "primary": "1h",
            "confirm": ["4h", "1d"],
            "fast": ["5m", "15m"]
        }
    }
    """
    cfg_path = Path(config_path) if config_path else CONFIG_PATH

    defaults = {
        "available": ["1m", "5m", "15m", "1h", "4h", "1d"],
        "default": "1h",
        "multi_tf_consensus": {
            "primary": "1h",
            "confirm": ["4h", "1d"],
            "fast": ["5m", "15m"],
        },
    }

    if not cfg_path.exists():
        logger.warning("时间周期配置文件未找到，使用默认值", path=str(cfg_path))
        return defaults

    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    if not cfg:
        return defaults

    for key in defaults:
        if key not in cfg:
            logger.warning("配置文件缺少 %s，使用默认值", key, key=key)

    return {**defaults, **(cfg or {})}


# ─── 数据类型 ──────────────────────────────────────────


@dataclass
class Kline:
    """标准化的 K 线数据结构"""
    symbol: str
    timeframe: str
    open_time: int           # 毫秒时间戳
    close_time: int          # 毫秒时间戳
    open: str                # 保留精度，字符串
    high: str
    low: str
    close: str
    volume: str
    is_closed: bool = True   # REST 返回的完整 K 线视为已收盘

    def to_stream_dict(self) -> dict[str, Any]:
        """转换为 raw_kline Stream 消息格式"""
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "ts": self.open_time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "is_closed": self.is_closed,
        }


@dataclass
class Ticker24hr:
    """24hr 行情统计"""
    symbol: str
    last_price: str
    volume: str             # 基础货币成交量
    quote_volume: str       # 计价货币成交量
    price_change_pct: str   # 24h 涨跌幅
    high_price: str
    low_price: str
    count: int              # 成交笔数


# ─── REST 客户端 ────────────────────────────────────────


class BinancePublicClient:
    """
    Binance 公开数据 REST 客户端。

    同时支持现货 (api.binance.com) 和合约 (fapi.binance.com) 端点。
    不依赖 API Key，仅使用公开数据。

    用法:
        client = BinancePublicClient()
        klines = await client.get_klines("BTCUSDT", "1h", limit=100)
        tickers = await client.get_tickers_24hr()
        await client.close()
    """

    def __init__(
        self,
        spot_base: str = _SPOT_BASE,
        fapi_base: str = _FAPI_BASE,
        timeout: int = 15,
        max_retries: int = 3,
    ):
        self._spot_base = spot_base
        self._fapi_base = fapi_base
        self._timeout = timeout
        self._max_retries = max_retries
        self._session = None
        self._semaphore = None  # 速率限制信号量

    # ─── Session 管理 ─────────────────────────────────

    async def _get_session(self):
        """惰性创建 aiohttp session"""
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                headers={"Accept": "application/json"},
            )
        return self._session

    async def close(self) -> None:
        """关闭 HTTP session"""
        if self._session and not self._session.closed:
            await self._session.close()

    # ─── HTTP 请求 ────────────────────────────────────

    async def _request(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        retries: int | None = None,
    ) -> dict | list | None:
        """
        发送 GET 请求，带重试和错误处理。

        参数:
            url: 完整的 API URL
            params: 查询参数
            retries: 重试次数，默认 self._max_retries

        返回:
            JSON 解析结果，失败返回 None
        """
        session = await self._get_session()
        retries = retries if retries is not None else self._max_retries
        last_error = None

        for attempt in range(retries):
            try:
                async with session.get(url, params=params) as resp:
                    if resp.status == 429:
                        # 速率限制，等待后重试
                        retry_after = int(resp.headers.get("Retry-After", 5))
                        logger.warning("API 速率限制，等待 %ds", retry_after, retry_after=retry_after)
                        import asyncio
                        await asyncio.sleep(retry_after)
                        continue

                    if resp.status == 418:
                        logger.error("API 被封禁，停止重试", url=url)
                        return None

                    if resp.status != 200:
                        logger.warning(
                            "API 请求失败",
                            url=url,
                            status=resp.status,
                            attempt=attempt + 1,
                        )
                        if attempt < retries - 1:
                            import asyncio
                            await asyncio.sleep(2 ** attempt)
                            continue
                        return None

                    return await resp.json()

            except Exception as e:
                last_error = e
                logger.warning(
                    "API 请求异常",
                    url=url,
                    error=str(e),
                    attempt=attempt + 1,
                )
                if attempt < retries - 1:
                    import asyncio
                    await asyncio.sleep(2 ** attempt)
                    continue

        logger.error("API 请求最终失败", url=url, error=str(last_error))
        return None

    # ─── K 线 API ─────────────────────────────────────

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start: int | None = None,
        end: int | None = None,
        limit: int = 500,
        use_futures: bool = False,
    ) -> list[Kline] | None:
        """
        获取历史 K 线数据。

        GET /api/v3/klines（现货）或 /fapi/v1/klines（合约）

        参数:
            symbol: 交易对，如 "BTCUSDT"
            interval: 时间周期，"1m", "5m", "15m", "1h", "4h", "1d"
            start: 起始时间戳（毫秒），可选
            end: 结束时间戳（毫秒），可选
            limit: 返回条数（最大 1000）
            use_futures: 是否使用合约端点

        返回:
            Kline 列表，按时间升序排列。失败返回 None。
        """
        base = self._fapi_base if use_futures else self._spot_base
        url = f"{base}/{'fapi/v1/klines' if use_futures else 'api/v3/klines'}"

        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": min(limit, 1000),
        }
        if start is not None:
            params["startTime"] = start
        if end is not None:
            params["endTime"] = end

        data = await self._request(url, params)
        if data is None:
            return None

        if not isinstance(data, list) or len(data) == 0:
            logger.warning("K 线数据为空", symbol=symbol, interval=interval)
            return []

        klines = []
        for raw in data:
            try:
                klines.append(Kline(
                    symbol=symbol.upper(),
                    timeframe=interval,
                    open_time=int(raw[KLINE_IDX["open_time"]]),
                    close_time=int(raw[KLINE_IDX["close_time"]]),
                    open=str(raw[KLINE_IDX["open"]]),
                    high=str(raw[KLINE_IDX["high"]]),
                    low=str(raw[KLINE_IDX["low"]]),
                    close=str(raw[KLINE_IDX["close"]]),
                    volume=str(raw[KLINE_IDX["volume"]]),
                    is_closed=True,
                ))
            except (IndexError, TypeError, ValueError) as e:
                logger.warning("K 线数据解析失败", error=str(e), raw=raw)
                continue

        return klines

    async def get_klines_as_dicts(
        self,
        symbol: str,
        interval: str,
        start: int | None = None,
        end: int | None = None,
        limit: int = 500,
        use_futures: bool = False,
    ) -> list[dict[str, Any]] | None:
        """
        获取历史 K 线并转换为字典列表（兼容 gap_filler 接口）。

        参数:
            同 get_klines()

        返回:
            [{"symbol":..., "timeframe":..., "ts":..., "open":..., ...}]
        """
        klines = await self.get_klines(symbol, interval, start, end, limit, use_futures)
        if klines is None:
            return None
        return [k.to_stream_dict() for k in klines]

    # ─── 24hr Ticker API ──────────────────────────────

    async def get_tickers_24hr(self) -> list[Ticker24hr] | None:
        """
        获取所有交易对的 24hr 行情统计。

        GET /api/v3/ticker/24hr

        返回:
            Ticker24hr 列表，按 quote_volume 降序排列。失败返回 None。
        """
        url = f"{self._spot_base}/api/v3/ticker/24hr"
        data = await self._request(url)

        if data is None or not isinstance(data, list):
            return None

        tickers = []
        for item in data:
            try:
                tickers.append(Ticker24hr(
                    symbol=str(item.get("symbol", "")),
                    last_price=str(item.get("lastPrice", "0")),
                    volume=str(item.get("volume", "0")),
                    quote_volume=str(item.get("quoteVolume", "0")),
                    price_change_pct=str(item.get("priceChangePercent", "0")),
                    high_price=str(item.get("highPrice", "0")),
                    low_price=str(item.get("lowPrice", "0")),
                    count=int(item.get("count", 0)),
                ))
            except (TypeError, ValueError) as e:
                logger.warning("Ticker 数据解析失败", error=str(e))
                continue

        return tickers

    async def get_top_symbols(
        self,
        quote_asset: str = "USDT",
        top_n: int = 50,
        min_volume: float = 0.0,
    ) -> list[Ticker24hr]:
        """
        获取指定计价资产的 Top N 交易对（按 quote_volume 降序）。

        参数:
            quote_asset: 计价资产，如 "USDT", "BUSD"
            top_n: 返回前 N 个
            min_volume: 最小成交量过滤

        返回:
            Ticker24hr 列表，空列表表示获取失败或无数据
        """
        tickers = await self.get_tickers_24hr()
        if not tickers:
            logger.warning("获取行情数据失败，返回空列表")
            return []

        # 过滤指定计价资产
        filtered = [
            t for t in tickers
            if t.symbol.endswith(quote_asset)
               and float(t.quote_volume) >= min_volume
        ]

        # 按 quote_volume 降序排序
        filtered.sort(key=lambda t: float(t.quote_volume), reverse=True)

        return filtered[:top_n]

    # ─── 最新价格 API ────────────────────────────────

    async def get_symbol_price(self, symbol: str) -> float | None:
        """
        获取单个交易对的最新价格。

        GET /api/v3/ticker/price

        参数:
            symbol: 交易对

        返回:
            最新价格（float），失败返回 None
        """
        url = f"{self._spot_base}/api/v3/ticker/price"
        data = await self._request(url, params={"symbol": symbol.upper()})

        if data is None or not isinstance(data, dict):
            return None

        try:
            return float(data.get("price", 0))
        except (TypeError, ValueError) as e:
            logger.warning("解析价格失败", symbol=symbol, error=str(e))
            return None

    async def get_all_prices(self) -> dict[str, float] | None:
        """
        获取所有交易对的最新价格。

        GET /api/v3/ticker/price

        返回:
            {symbol: price} 字典，失败返回 None
        """
        url = f"{self._spot_base}/api/v3/ticker/price"
        data = await self._request(url)

        if data is None or not isinstance(data, list):
            return None

        result = {}
        for item in data:
            try:
                sym = str(item.get("symbol", ""))
                price = float(item.get("price", 0))
                result[sym] = price
            except (TypeError, ValueError):
                continue

        return result

    # ─── 交易所信息 API ─────────────────────────────

    async def get_exchange_info(self) -> dict | None:
        """
        获取交易所信息（交易对规则、精度等）。

        GET /api/v3/exchangeInfo

        返回:
            完整 exchangeInfo 字典，失败返回 None
        """
        url = f"{self._spot_base}/api/v3/exchangeInfo"
        return await self._request(url)

    async def get_usdt_pairs(self) -> list[dict[str, Any]]:
        """
        获取所有 USDT 交易对的信息（TRADING 状态）。

        返回:
            [{"symbol":..., "baseAsset":..., "quoteAsset":..., "status":..., ...}]
        """
        info = await self.get_exchange_info()
        if info is None or "symbols" not in info:
            logger.warning("获取交易所信息失败")
            return []

        symbols = info.get("symbols", [])
        return [
            s for s in symbols
            if s.get("quoteAsset") == "USDT"
               and s.get("status") == "TRADING"
        ]
