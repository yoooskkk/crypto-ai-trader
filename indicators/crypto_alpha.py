"""
模块名称: crypto_alpha.py
所属层级: 指标计算层 (Indicators)
输入来源: OHLCV DataFrame + Binance Futures REST API（资金费率/未平仓合约）
输出去向: 追加交易所特有 alpha 指标的 DataFrame
关键依赖: pandas, numpy, aiohttp, structlog, yaml

修订记录:
- v1.0: 初始实现，FUNDING_RATE + OI_DELTA(24h) + CVD_DELTA(100bar)
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import structlog
import yaml

logger = structlog.get_logger(__name__)

_CONFIG_DIR = Path(__file__).parent.parent / "config"
CONFIG_PATH = _CONFIG_DIR / "indicators.yml"

# ─── Binance Futures API 常量 ────────────────────────────────

# 通过环境变量可覆盖，用于代理/镜像/测试网
_BINANCE_FAPI_BASE = (
    os.environ.get("BINANCE_FAPI_BASE")
    or os.environ.get("CRYPTO_ALPHA_BINANCE_URL")
    or "https://fapi.binance.com"
)
_BINANCE_FDATA_BASE = _BINANCE_FAPI_BASE  # 历史数据同域

# HTTP/HTTPS 代理（生产环境需要时设置）
_HTTP_PROXY = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
_HTTPS_PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""

# API 请求超时与重试
_DEFAULT_TIMEOUT_S = int(os.environ.get("CRYPTO_ALPHA_TIMEOUT", "15"))
_DEFAULT_RETRY_COUNT = int(os.environ.get("CRYPTO_ALPHA_RETRY", "3"))
_DEFAULT_RETRY_DELAY_S = float(os.environ.get("CRYPTO_ALPHA_RETRY_DELAY", "1.0"))


# ─── 配置读取 ────────────────────────────────────────────────

@lru_cache(maxsize=1)
def load_crypto_alpha_params(config_path: str | Path | None = None) -> dict[str, Any]:
    """
    从 config/indicators.yml 的 crypto_alpha 段读取参数。

        返回结构:
    {
        "funding_rate_source": "binance",
        "oi_delta_period": 24,         # 小时
        "cvd_lookback": 100,           # K 线根数
        "timeout": 15,                 # API 请求超时秒数
        "retry_count": 3,              # 失败重试次数
        "proxy": "",                  # HTTP 代理 URL
    }
    """
    cfg_path = Path(config_path) if config_path else CONFIG_PATH

    if not cfg_path.exists():
        logger.warning("配置文件未找到，使用默认参数", path=str(cfg_path))
        return {
            "funding_rate_source": "binance",
            "oi_delta_period": 24,
            "cvd_lookback": 100,
            "timeout": _DEFAULT_TIMEOUT_S,
            "retry_count": _DEFAULT_RETRY_COUNT,
            "proxy": "",
        }

    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    ca_cfg = cfg.get("crypto_alpha", {})
    defaults = {
        "funding_rate_source": "binance",
        "oi_delta_period": 24,
        "cvd_lookback": 100,
        "timeout": _DEFAULT_TIMEOUT_S,
        "retry_count": _DEFAULT_RETRY_COUNT,
        "proxy": "",
    }

    for key in defaults:
        if key not in ca_cfg:
            logger.warning("配置文件缺少 crypto_alpha.%s，使用默认值", key, key=key, default=defaults[key])

    return {**defaults, **ca_cfg}


# ─── Binance Futures 公开 REST 客户端 ─────────────────────

@dataclass
class FundingRateResult:
    """资金费率查询结果"""
    funding_rate: float  # 当前资金费率（如 0.0001 = 0.01%）
    mark_price: float    # 标记价格
    next_funding_time: int  # 下次结算时间戳（毫秒）


@dataclass
class OpenInterestResult:
    """未平仓合约查询结果"""
    open_interest: float  # 当前未平仓量
    time: int             # 查询时间戳（毫秒）


class BinanceFuturesPublicClient:
    """
    Binance USD-M Futures 公开数据客户端。
    仅使用公开 REST 端点，无需 API Key。

    支持:
    - 环境变量覆盖 API URL（BINANCE_FAPI_BASE / CRYPTO_ALPHA_BINANCE_URL）
    - 代理配置（HTTP_PROXY / HTTPS_PROXY 环境变量）
    - 可配置超时与重试
    - 自动 retry

    端点文档:
    - 资金费率: GET /fapi/v1/premiumIndex
    - 未平仓量: GET /fapi/v1/openInterest
    - 未平仓历史: GET /futures/data/openInterestHist
    速率限制: 2400 次/分钟（公开端点）
    """

    def __init__(
        self,
        base_url: str = _BINANCE_FAPI_BASE,
        timeout: int = _DEFAULT_TIMEOUT_S,
        retry_count: int = _DEFAULT_RETRY_COUNT,
        retry_delay: float = _DEFAULT_RETRY_DELAY_S,
        proxy: str | None = None,
    ):
        """
        初始化客户端。

        参数:
            base_url: Binance API 基础 URL（默认 https://fapi.binance.com）
            timeout: 单次请求超时秒数
            retry_count: 失败重试次数
            retry_delay: 重试间隔秒数
            proxy: 代理 URL，如 "http://proxy.example.com:8080"。
                   若为 None，自动读取 HTTP_PROXY/HTTPS_PROXY 环境变量
        """
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._retry_count = retry_count
        self._retry_delay = retry_delay
        self._proxy = proxy or _HTTPS_PROXY or _HTTP_PROXY or None
        self._session = None

    async def _get_session(self):
        """惰性创建 aiohttp session（避免未使用时创建）"""
        if self._session is None or self._session.closed:
            import aiohttp

            connector = None
            if self._proxy:
                logger.info("使用代理连接 Binance API", proxy=self._proxy)
                # TCPConnector 可配合 proxy 参数使用
                connector = aiohttp.TCPConnector(verify_ssl=True)

            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                headers={"Accept": "application/json"},
                connector=connector,
            )
        return self._session

    async def close(self) -> None:
        """关闭 HTTP session"""
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        params: dict | None = None,
    ) -> dict | list | None:
        """
        带重试机制的 HTTP 请求。

        参数:
            method: HTTP 方法（"GET" 等）
            url: 完整请求 URL
            params: URL 查询参数

        返回:
            解析后的 JSON 响应，全部失败返回 None
        """
        session = await self._get_session()

        for attempt in range(1, self._retry_count + 1):
            try:
                kwargs: dict[str, Any] = {"params": params}
                if self._proxy:
                    kwargs["proxy"] = self._proxy

                async with session.request(method, url, **kwargs) as resp:
                    if resp.status == 200:
                        return await resp.json()

                    # 429 = 速率限制，504 = 网关超时，需要更长延迟
                    if resp.status in (429, 503, 504):
                        wait = self._retry_delay * (2 ** (attempt - 1)) * 2
                        logger.warning(
                            "Binance API 临时错误",
                            status=resp.status,
                            attempt=attempt,
                            wait_s=round(wait, 1),
                            url=url,
                        )
                        if attempt < self._retry_count:
                            await asyncio.sleep(wait)
                            continue
                    else:
                        logger.warning(
                            "Binance API 返回错误",
                            status=resp.status,
                            attempt=attempt,
                            url=url,
                        )
                        return None

            except asyncio.TimeoutError:
                wait = self._retry_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Binance API 请求超时",
                    attempt=attempt,
                    wait_s=round(wait, 1),
                    url=url,
                )
                if attempt < self._retry_count:
                    await asyncio.sleep(wait)
                    continue
                return None

            except Exception as e:
                logger.error(
                    "Binance API 请求异常",
                    attempt=attempt,
                    error=str(e),
                    url=url,
                )
                if attempt < self._retry_count:
                    await asyncio.sleep(self._retry_delay)
                    continue
                return None

        return None

    async def get_premium_index(self, symbol: str) -> FundingRateResult | None:
        """
        获取当前资金费率和标记价格。
        GET /fapi/v1/premiumIndex

        参数:
            symbol: 交易对，如 "BTCUSDT"

        返回:
            FundingRateResult，失败返回 None
        """
        url = f"{self._base_url}/fapi/v1/premiumIndex"
        data = await self._request_with_retry("GET", url, params={"symbol": symbol})

        if data is None:
            return None

        try:
            return FundingRateResult(
                funding_rate=float(data.get("lastFundingRate", 0)),
                mark_price=float(data.get("markPrice", 0)),
                next_funding_time=int(data.get("nextFundingTime", 0)),
            )
        except (TypeError, ValueError) as e:
            logger.error("解析资金费率响应失败", error=str(e), symbol=symbol)
            return None

    async def get_open_interest(self, symbol: str) -> OpenInterestResult | None:
        """
        获取当前未平仓合约量。
        GET /fapi/v1/openInterest

        参数:
            symbol: 交易对，如 "BTCUSDT"

        返回:
            OpenInterestResult，失败返回 None
        """
        url = f"{self._base_url}/fapi/v1/openInterest"
        data = await self._request_with_retry("GET", url, params={"symbol": symbol})

        if data is None:
            return None

        try:
            return OpenInterestResult(
                open_interest=float(data.get("openInterest", 0)),
                time=int(data.get("time", 0)),
            )
        except (TypeError, ValueError) as e:
            logger.error("解析 OI 响应失败", error=str(e), symbol=symbol)
            return None

    async def get_open_interest_history(
        self, symbol: str, period: str = "1h", limit: int = 30
    ) -> list[dict] | None:
        """
        获取历史未平仓合约量（用于计算 24h delta）。
        GET /futures/data/openInterestHist

        参数:
            symbol: 交易对
            period: 数据粒度（"5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"）
            limit: 返回条数（最大 500）

        返回:
            [{"symbol":"BTCUSDT","sumOpenInterest":"...","sumOpenInterestValue":"...","timestamp":...}]
        """
        url = f"{self._base_url}/futures/data/openInterestHist"
        data = await self._request_with_retry("GET", url, params={
            "symbol": symbol,
            "period": period,
            "limit": min(limit, 500),
        })

        if data is None:
            return None

        # openInterestHist 返回的是列表（不同于其他端点）
        if isinstance(data, list):
            return data

        logger.warning("OI 历史返回格式异常", symbol=symbol, type=type(data).__name__)
        return None


# ─── 指标计算函数 ────────────────────────────────────────────

def compute_cvd_delta(df: pd.DataFrame, lookback: int = 100) -> pd.Series:
    """
    计算 CVD_DELTA（累积成交量差值的滚动和）。

    使用 OHLCV 数据近似每根 K 线的买卖压力差:
        buy_pressure  = volume * (close - low) / (high - low)
        sell_pressure = volume * (high - close) / (high - low)
        delta = buy_pressure - sell_pressure
        CVD = rolling_sum(delta, lookback)

    当 high == low 时（极端行情），delta = 0。

    参数:
        df: 含 high, low, close, volume 的 DataFrame
        lookback: 滚动求和窗口（标准 100）

    返回:
        名为 CVD_DELTA_{lookback} 的 Series
    """
    for col in ["high", "low", "close", "volume"]:
        if col not in df.columns:
            logger.warning("DataFrame 缺少 '%s' 列", col, column=col)
            return pd.Series(index=df.index, dtype=float, name=f"CVD_DELTA_{lookback}")

    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    volume = df["volume"].values

    # 防止除零
    price_range = high - low
    price_range = np.where(price_range == 0, 1.0, price_range)

    # 买卖压力差
    buy_pressure = volume * (close - low) / price_range
    sell_pressure = volume * (high - close) / price_range
    delta = buy_pressure - sell_pressure

    # 滚动求和
    cvd = pd.Series(delta, index=df.index).rolling(window=lookback, min_periods=1).sum()
    cvd.name = f"CVD_DELTA_{lookback}"
    return cvd


def compute_oi_delta_from_hist(
    hist_data: list[dict] | None,
    current_oi: float | None,
) -> float | None:
    """
    从历史 OI 数据和当前 OI 计算 24h 变化量。

    参数:
        hist_data: openInterestHist 返回的数据列表（时间升序）
        current_oi: 当前未平仓量

    返回:
        (当前 OI - 24h 前 OI) / 24h 前 OI 的百分比变化，数据不足返回 None
    """
    if not hist_data or current_oi is None:
        return None

    try:
        # 取最早一条作为 24h 前基准
        oldest_oi = float(hist_data[0].get("sumOpenInterest", 0))
        if oldest_oi == 0:
            return None
        return (current_oi - oldest_oi) / oldest_oi * 100  # 百分比变化
    except (IndexError, TypeError, ValueError) as e:
        logger.warning("计算 OI delta 失败", error=str(e))
        return None


def compute_oi_delta_simple(df: pd.DataFrame, period: int = 24) -> pd.Series:
    """
    备用方法：使用 DataFrame 内的 volume 变化近似 OI delta。
    当 Binance API 不可用时使用。

    实际是基于成交量的变化率，作为 OI delta 的 proxy。

    参数:
        df: 含 volume 列的 DataFrame
        period: 回溯周期（K 线根数）

    返回:
        名为 OI_DELTA_PROXY_{period}h 的 Series
    """
    if "volume" not in df.columns:
        logger.warning("DataFrame 缺少 'volume' 列")
        return pd.Series(index=df.index, dtype=float, name=f"OI_DELTA_PROXY_{period}h")

    vol_shifted = df["volume"].shift(period)
    delta = (df["volume"] - vol_shifted) / vol_shifted.replace(0, np.nan) * 100
    delta.name = f"OI_DELTA_PROXY_{period}h"
    return delta


# ─── 主计算函数 ────────────────────────────────────────────────

async def compute_crypto_alpha(
    df: pd.DataFrame,
    symbol: str = "BTCUSDT",
    cfg: dict | None = None,
    binance_client: BinanceFuturesPublicClient | None = None,
) -> pd.DataFrame:
    """
    计算交易所特有 alpha 指标（资金费率 + OI delta + CVD delta）。

    此函数是 async 的，因为需要调用 Binance REST API。

    参数:
        df: OHLCV DataFrame（列: open, high, low, close, volume）
        symbol: 交易对，如 "BTCUSDT"
        cfg: indicators.yml 的 crypto_alpha 段配置
        binance_client: 可复用的 Binance API 客户端。为 None 时自动创建。

    返回:
        追加了以下列的 DataFrame:
        - FUNDING_RATE（最新资金费率，全部行填充相同值）
        - OI_DELTA_24h（未平仓合约 24h 变化百分比）
        - CVD_DELTA_{lookback}（累积成交量差值）

    注意:
        - 如果 API 调用失败，FUNDING_RATE 和 OI_DELTA_24h 列为 NaN
        - CVD_DELTA 基于 OHLCV 近似计算，不需要 API
        - 纯函数，不修改输入 df
    """
    df = df.copy()

    if cfg is None:
        cfg = load_crypto_alpha_params()

    for col in ["high", "low", "close", "volume"]:
        if col not in df.columns:
            logger.warning("DataFrame 缺少必要列 '%s'", col, column=col)
            return df

        if not pd.api.types.is_numeric_dtype(df["close"]):
            logger.warning("'close' 列必须为数值类型")
            return df

    own_client = False
    if binance_client is None:
        # 从配置读取代理设置
        proxy_url = cfg.get("proxy", "")
        timeout = cfg.get("timeout", _DEFAULT_TIMEOUT_S)
        retry_count = cfg.get("retry_count", _DEFAULT_RETRY_COUNT)
        binance_client = BinanceFuturesPublicClient(
            timeout=timeout,
            retry_count=retry_count,
            proxy=proxy_url or None,
        )
        own_client = True

    try:
        # 1. CVD_DELTA（纯 OHLCV 计算，不需要 API）
        lookback = cfg.get("cvd_lookback", 100)
        df[f"CVD_DELTA_{lookback}"] = compute_cvd_delta(df, lookback)

        # 2. 资金费率（需要 API）
        funding_result = await binance_client.get_premium_index(symbol)
        if funding_result is not None:
            df["FUNDING_RATE"] = funding_result.funding_rate
        else:
            logger.warning("资金费率获取失败，FUNDING_RATE 设为 NaN", symbol=symbol)
            df["FUNDING_RATE"] = np.nan

        # 3. OI delta 24h（需要 API）
        oi_period_hours = cfg.get("oi_delta_period", 24)
        current_oi_result = await binance_client.get_open_interest(symbol)

        if current_oi_result is not None:
            # 获取 24h 前的历史 OI（按小时粒度取足够条数）
            hist_limit = max(oi_period_hours + 5, 30)  # 多取几条确保覆盖
            hist_oi = await binance_client.get_open_interest_history(
                symbol, period="1h", limit=hist_limit
            )

            oi_delta = compute_oi_delta_from_hist(hist_oi, current_oi_result.open_interest)

            if oi_delta is not None:
                df["OI_DELTA_24h"] = oi_delta
            else:
                logger.warning("OI delta 计算失败，使用 volume proxy", symbol=symbol)
                # 降级：用 volume proxy
                df[f"OI_DELTA_PROXY_{oi_period_hours}h"] = compute_oi_delta_simple(df, oi_period_hours)
                df["OI_DELTA_24h"] = np.nan
        else:
            logger.warning("当前 OI 获取失败，OI_DELTA_24h 使用 volume proxy", symbol=symbol)
            df[f"OI_DELTA_PROXY_{oi_period_hours}h"] = compute_oi_delta_simple(df, oi_period_hours)
            df["OI_DELTA_24h"] = np.nan

    except Exception as e:
        logger.error("crypto_alpha 计算异常", error=str(e))
        # 至少计算 CVD（不依赖 API）
        if f"CVD_DELTA_{cfg.get('cvd_lookback', 100)}" not in df.columns:
            lookback = cfg.get("cvd_lookback", 100)
            df[f"CVD_DELTA_{lookback}"] = compute_cvd_delta(df, lookback)
    finally:
        if own_client:
            await binance_client.close()

    return df


# ─── 同步包装器（供不需要 async 的调用方使用） ─────────────

def compute_cvd_only(df: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    """
    仅计算 CVD_DELTA（不需要 API 调用的同步版本）。

    参数:
        df: OHLCV DataFrame
        cfg: crypto_alpha 配置

    返回:
        追加了 CVD_DELTA 列的 DataFrame
    """
    if cfg is None:
        cfg = load_crypto_alpha_params()

    df = df.copy()
    lookback = cfg.get("cvd_lookback", 100)
    df[f"CVD_DELTA_{lookback}"] = compute_cvd_delta(df, lookback)
    return df
