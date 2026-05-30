"""
测试: data/rest_client.py — BinancePublicClient

测试策略:
    - 用 unittest.mock 模拟 aiohttp ClientSession，不发起真实 HTTP 请求
    - 覆盖 K 线解析、Ticker 解析、排名、错误处理、重试
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data.rest_client import (
    BinancePublicClient,
    Kline,
    Ticker24hr,
    KLINE_IDX,
)


# ─── Helpers ───────────────────────────────────────────


def _mock_http_response(status: int = 200, json_data=None):
    """
    构造一个模拟的 aiohttp response 对象，支持 ``async with session.get(...) as resp:`` 语法。

    参数:
        status: HTTP 状态码
        json_data: await resp.json() 的返回值

    返回:
        (context_manager_mock, response_mock) 的二元组
        - cm_mock: async context manager，其 __aenter__ 返回 response
        - resp_mock: response mock，可额外配置 .headers 等
    """
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data)
    resp.headers = {}

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)

    return cm, resp


def _patch_session(mock_session, cm):
    """
    将 mock_session.get 配置为返回指定的 async context manager。

    参数:
        mock_session: MagicMock 模拟的 session
        cm: async context manager（由 _mock_http_response 返回的第一个元素）
    """
    mock_session.get.return_value = cm


# ─── Fixtures ──────────────────────────────────────────


@pytest.fixture
def client() -> BinancePublicClient:
    """返回一个 BinancePublicClient 实例（不连接真实 API）"""
    return BinancePublicClient(timeout=5, max_retries=1)


@pytest.fixture
def mock_session():
    """
    创建一个模拟的 aiohttp ClientSession。
    使用 MagicMock 以便精确控制 get() 的 async context manager 行为。
    """
    session = MagicMock()
    session.closed = False
    return session


# ─── 模拟数据 ──────────────────────────────────────────


def _mock_kline_raw(
    open_time: int = 1700000000000,
    close_time: int = 1700003600000,
    open: str = "42000.00",
    high: str = "42500.00",
    low: str = "41800.00",
    close: str = "42200.00",
    volume: str = "1234.56",
    trades: int = 12345,
) -> list:
    """生成一条模拟的 Binance K 线原始数据（数组格式）"""
    return [
        open_time,
        open,
        high,
        low,
        close,
        volume,
        close_time,
        "5000000.00",   # quote volume
        trades,
        "600.00",       # taker buy base
        "2500000.00",   # taker buy quote
        "0",            # ignore
    ]


SAMPLE_KLINES_RESPONSE = [
    _mock_kline_raw(
        open_time=1700000000000 + i * 3600000,
        close_time=1700003600000 + i * 3600000,
        open=f"{42000 + i * 100}.00",
        close=f"{42100 + i * 50}.00",
        volume=f"{1000 + i * 10}.00",
    )
    for i in range(5)
]

SAMPLE_TICKERS_RESPONSE = [
    {
        "symbol": "BTCUSDT",
        "lastPrice": "42000.00",
        "volume": "50000.00",
        "quoteVolume": "2100000000.00",
        "priceChangePercent": "2.50",
        "highPrice": "43000.00",
        "lowPrice": "41000.00",
        "count": 123456,
    },
    {
        "symbol": "ETHUSDT",
        "lastPrice": "2500.00",
        "volume": "300000.00",
        "quoteVolume": "750000000.00",
        "priceChangePercent": "-1.20",
        "highPrice": "2600.00",
        "lowPrice": "2450.00",
        "count": 78901,
    },
    {
        "symbol": "SOLUSDT",
        "lastPrice": "150.00",
        "volume": "2000000.00",
        "quoteVolume": "300000000.00",
        "priceChangePercent": "5.80",
        "highPrice": "155.00",
        "lowPrice": "142.00",
        "count": 45678,
    },
    {
        "symbol": "XRPUSDT",
        "lastPrice": "0.50",
        "volume": "100000000.00",
        "quoteVolume": "50000000.00",
        "priceChangePercent": "-0.50",
        "highPrice": "0.52",
        "lowPrice": "0.49",
        "count": 12345,
    },
    {
        "symbol": "BNBUSDT",
        "lastPrice": "600.00",
        "volume": "100000.00",
        "quoteVolume": "60000000.00",
        "priceChangePercent": "1.20",
        "highPrice": "610.00",
        "lowPrice": "590.00",
        "count": 23456,
    },
]


# ─── K 线解析测试 ──────────────────────────────────


class TestKlineParsing:
    """K 线数据解析测试"""

    async def test_parse_single_kline(self, client, mock_session):
        """测试单条 K 线解析为标准格式"""
        with patch.object(client, "_get_session", return_value=mock_session):
            cm, _ = _mock_http_response(200, [SAMPLE_KLINES_RESPONSE[0]])
            _patch_session(mock_session, cm)

            klines = await client.get_klines("BTCUSDT", "1h", limit=1)

            assert klines is not None
            assert len(klines) == 1
            k = klines[0]
            assert isinstance(k, Kline)
            assert k.symbol == "BTCUSDT"
            assert k.timeframe == "1h"
            assert k.open_time == 1700000000000
            assert k.close_time == 1700003600000
            assert k.open == "42000.00"
            assert k.high == "42500.00"
            assert k.low == "41800.00"
            assert k.close == "42100.00"  # 来自 SAMPLE_KLINES_RESPONSE[0]: 42100 + 0*50
            assert k.volume == "1000.00"  # 来自 SAMPLE_KLINES_RESPONSE[0]: 1000 + 0*10
            assert k.is_closed is True

    async def test_parse_multiple_klines(self, client, mock_session):
        """测试多条 K 线批量解析"""
        with patch.object(client, "_get_session", return_value=mock_session):
            cm, _ = _mock_http_response(200, SAMPLE_KLINES_RESPONSE)
            _patch_session(mock_session, cm)

            klines = await client.get_klines("BTCUSDT", "1h", limit=5)

            assert klines is not None
            assert len(klines) == 5
            # 验证时间递增
            times = [k.open_time for k in klines]
            assert times == sorted(times)

    async def test_to_stream_dict(self, client, mock_session):
        """测试 Kline → raw_kline Stream 消息格式转换"""
        with patch.object(client, "_get_session", return_value=mock_session):
            cm, _ = _mock_http_response(200, [SAMPLE_KLINES_RESPONSE[0]])
            _patch_session(mock_session, cm)

            klines = await client.get_klines("BTCUSDT", "1h", limit=1)
            assert klines is not None

            d = klines[0].to_stream_dict()
            assert d["symbol"] == "BTCUSDT"
            assert d["timeframe"] == "1h"
            assert d["ts"] == 1700000000000
            assert d["open"] == "42000.00"
            assert d["high"] == "42500.00"
            assert d["low"] == "41800.00"
            assert d["close"] == "42100.00"  # 匹配 sample 数据
            assert d["volume"] == "1000.00"  # 匹配 sample 数据
            assert d["is_closed"] is True
            # 验证 Stream 消息必须的字段
            required = {"symbol", "timeframe", "ts", "open", "high", "low", "close", "volume", "is_closed"}
            assert required.issubset(d.keys())

    async def test_get_klines_as_dicts(self, client, mock_session):
        """测试 get_klines_as_dicts 兼容接口"""
        with patch.object(client, "_get_session", return_value=mock_session):
            cm, _ = _mock_http_response(200, [SAMPLE_KLINES_RESPONSE[0]])
            _patch_session(mock_session, cm)

            dicts = await client.get_klines_as_dicts("BTCUSDT", "1h", limit=1)

            assert dicts is not None
            assert len(dicts) == 1
            assert dicts[0]["symbol"] == "BTCUSDT"
            assert dicts[0]["ts"] == 1700000000000

    async def test_empty_response(self, client, mock_session):
        """测试空数据返回"""
        with patch.object(client, "_get_session", return_value=mock_session):
            cm, _ = _mock_http_response(200, [])
            _patch_session(mock_session, cm)

            klines = await client.get_klines("BTCUSDT", "1h")
            assert klines is not None
            assert len(klines) == 0

    async def test_malformed_kline(self, client, mock_session):
        """测试异常 K 线数据（字段缺失）— 应跳过该条"""
        with patch.object(client, "_get_session", return_value=mock_session):
            malformed = [SAMPLE_KLINES_RESPONSE[0][:3]]  # 只有 3 个字段，不是 12 个
            cm, _ = _mock_http_response(200, malformed)
            _patch_session(mock_session, cm)

            klines = await client.get_klines("BTCUSDT", "1h", limit=1)
            assert klines is not None
            assert len(klines) == 0  # 异常数据被跳过


# ─── Ticker 解析测试 ────────────────────────────────


class TestTickerParsing:
    """Ticker 行情数据解析测试"""

    async def test_get_tickers_24hr(self, client, mock_session):
        """测试获取全部 24hr ticker"""
        with patch.object(client, "_get_session", return_value=mock_session):
            cm, _ = _mock_http_response(200, SAMPLE_TICKERS_RESPONSE)
            _patch_session(mock_session, cm)

            tickers = await client.get_tickers_24hr()

            assert tickers is not None
            assert len(tickers) == 5
            assert all(isinstance(t, Ticker24hr) for t in tickers)

    async def test_ticker_fields(self, client, mock_session):
        """验证 Ticker24hr 字段解析正确"""
        with patch.object(client, "_get_session", return_value=mock_session):
            cm, _ = _mock_http_response(200, [SAMPLE_TICKERS_RESPONSE[0]])
            _patch_session(mock_session, cm)

            tickers = await client.get_tickers_24hr()
            assert tickers is not None
            btc = tickers[0]
            assert btc.symbol == "BTCUSDT"
            assert btc.last_price == "42000.00"
            assert btc.quote_volume == "2100000000.00"
            assert btc.price_change_pct == "2.50"
            assert btc.count == 123456

    async def test_get_top_symbols_ranking(self, client, mock_session):
        """测试 Top N 排名按 quote_volume 降序"""
        with patch.object(client, "_get_session", return_value=mock_session):
            cm, _ = _mock_http_response(200, SAMPLE_TICKERS_RESPONSE)
            _patch_session(mock_session, cm)

            top = await client.get_top_symbols(top_n=3)

            assert len(top) == 3
            volumes = [float(t.quote_volume) for t in top]
            assert volumes == sorted(volumes, reverse=True)
            assert top[0].symbol == "BTCUSDT"
            assert top[1].symbol == "ETHUSDT"

    async def test_get_top_symbols_filter_by_quote(self, client, mock_session):
        """测试按计价资产过滤"""
        with patch.object(client, "_get_session", return_value=mock_session):
            tickers = SAMPLE_TICKERS_RESPONSE + [{
                "symbol": "BTCETH",
                "lastPrice": "16.80",
                "volume": "1000.00",
                "quoteVolume": "16800.00",
                "priceChangePercent": "0.50",
                "highPrice": "17.00",
                "lowPrice": "16.50",
                "count": 100,
            }]
            cm, _ = _mock_http_response(200, tickers)
            _patch_session(mock_session, cm)

            top = await client.get_top_symbols(quote_asset="USDT", top_n=10)
            assert all(t.symbol.endswith("USDT") for t in top)
            assert len(top) == 5

    async def test_get_top_symbols_min_volume_filter(self, client, mock_session):
        """测试最小成交量过滤"""
        with patch.object(client, "_get_session", return_value=mock_session):
            cm, _ = _mock_http_response(200, SAMPLE_TICKERS_RESPONSE)
            _patch_session(mock_session, cm)

            top = await client.get_top_symbols(min_volume=1_000_000_000, top_n=10)
            assert len(top) == 1
            assert top[0].symbol == "BTCUSDT"


# ─── 错误处理测试 ──────────────────────────────────


class TestErrorHandling:
    """错误处理和重试机制测试"""

    async def test_rate_limit_429_retry(self, client, mock_session):
        """测试 429 速率限制 → 等待后重试 → 成功"""
        # 429 场景需要至少 2 次尝试（首次 429 + 重试）
        # 创建临时 client 覆盖 max_retries
        client2 = BinancePublicClient(timeout=5, max_retries=2)
        with patch.object(client2, "_get_session", return_value=mock_session):
            cm1, resp1 = _mock_http_response(429, None)
            resp1.headers = {"Retry-After": "1"}
            cm2, _ = _mock_http_response(200, [SAMPLE_KLINES_RESPONSE[0]])

            mock_session.get.side_effect = [cm1, cm2]

            klines = await client2.get_klines("BTCUSDT", "1h", limit=1)
            assert klines is not None
            assert len(klines) == 1

    async def test_418_banned(self, client, mock_session):
        """测试 418 被封禁 → 返回 None 不再重试"""
        with patch.object(client, "_get_session", return_value=mock_session):
            cm, _ = _mock_http_response(418, None)
            _patch_session(mock_session, cm)

            klines = await client.get_klines("BTCUSDT", "1h", limit=1)
            assert klines is None

    async def test_500_server_error(self, client, mock_session):
        """测试 500 服务器错误 → 重试后返回 None"""
        with patch.object(client, "_get_session", return_value=mock_session):
            cm, _ = _mock_http_response(500, None)
            _patch_session(mock_session, cm)

            klines = await client.get_klines("BTCUSDT", "1h", limit=1)
            assert klines is None

    async def test_network_timeout(self, client, mock_session):
        """测试网络超时 → 重试后返回 None"""
        with patch.object(client, "_get_session", return_value=mock_session):
            # 当调用 get() 时直接抛异常（不是 async context manager 内）
            mock_session.get.side_effect = TimeoutError("Connection timed out")

            klines = await client.get_klines("BTCUSDT", "1h", limit=1)
            assert klines is None

    async def test_http_params_format(self, client, mock_session):
        """验证 HTTP 请求参数格式正确"""
        with patch.object(client, "_get_session", return_value=mock_session):
            cm, _ = _mock_http_response(200, [SAMPLE_KLINES_RESPONSE[0]])
            _patch_session(mock_session, cm)

            await client.get_klines("BTCUSDT", "1h", start=1000, end=2000, limit=100)

            call_kwargs = mock_session.get.call_args
            assert call_kwargs is not None
            args, kwargs = call_kwargs
            params = kwargs.get("params", {})
            assert params["symbol"] == "BTCUSDT"
            assert params["interval"] == "1h"
            assert params["startTime"] == 1000
            assert params["endTime"] == 2000
            assert params["limit"] == 100


# ─── 价格和交易所信息测试 ──────────────────────────


class TestPriceAndExchange:
    """价格查询和交易所信息测试"""

    async def test_get_symbol_price(self, client, mock_session):
        """测试获取单个币种价格"""
        with patch.object(client, "_get_session", return_value=mock_session):
            cm, _ = _mock_http_response(200, {"symbol": "BTCUSDT", "price": "42000.00"})
            _patch_session(mock_session, cm)

            price = await client.get_symbol_price("BTCUSDT")
            assert price == 42000.00

    async def test_get_symbol_price_failure(self, client, mock_session):
        """测试获取价格失败 → 返回 None"""
        with patch.object(client, "_get_session", return_value=mock_session):
            cm, _ = _mock_http_response(400, None)
            _patch_session(mock_session, cm)

            price = await client.get_symbol_price("INVALID")
            assert price is None

    async def test_get_all_prices(self, client, mock_session):
        """测试获取全部价格"""
        with patch.object(client, "_get_session", return_value=mock_session):
            cm, _ = _mock_http_response(200, [
                {"symbol": "BTCUSDT", "price": "42000.00"},
                {"symbol": "ETHUSDT", "price": "2500.00"},
            ])
            _patch_session(mock_session, cm)

            prices = await client.get_all_prices()
            assert prices is not None
            assert prices["BTCUSDT"] == 42000.00
            assert prices["ETHUSDT"] == 2500.00

    async def test_get_exchange_info(self, client, mock_session):
        """测试获取交易所信息"""
        with patch.object(client, "_get_session", return_value=mock_session):
            cm, _ = _mock_http_response(200, {
                "timezone": "UTC",
                "symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "quoteAsset": "USDT"}],
            })
            _patch_session(mock_session, cm)

            info = await client.get_exchange_info()
            assert info is not None
            assert info["timezone"] == "UTC"

    async def test_get_usdt_pairs(self, client, mock_session):
        """测试获取 USDT 交易对列表"""
        with patch.object(client, "_get_session", return_value=mock_session):
            cm, _ = _mock_http_response(200, {
                "symbols": [
                    {"symbol": "BTCUSDT", "status": "TRADING", "quoteAsset": "USDT"},
                    {"symbol": "ETHUSDT", "status": "TRADING", "quoteAsset": "USDT"},
                    {"symbol": "BTCETH", "status": "TRADING", "quoteAsset": "ETH"},
                    {"symbol": "XRPUSDT", "status": "BREAK", "quoteAsset": "USDT"},
                ]
            })
            _patch_session(mock_session, cm)

            pairs = await client.get_usdt_pairs()
            assert len(pairs) == 2
            assert pairs[0]["symbol"] == "BTCUSDT"
            assert pairs[1]["symbol"] == "ETHUSDT"


# ─── 资源管理测试 ──────────────────────────────────


class TestResourceManagement:
    """资源管理测试（Session 生命周期）"""

    async def test_session_lazy_creation(self, client):
        """验证 session 是惰性创建的（初始化时不存在）"""
        assert client._session is None

    async def test_session_reuse(self, client, mock_session):
        """验证 session 被复用"""
        with patch.object(client, "_get_session", return_value=mock_session):
            cm, _ = _mock_http_response(200, [SAMPLE_KLINES_RESPONSE[0]])
            _patch_session(mock_session, cm)

            await client.get_klines("BTCUSDT", "1h", limit=1)
            await client.get_klines("ETHUSDT", "1h", limit=1)

            assert mock_session.get.call_count == 2

    async def test_close_session(self, client):
        """验证 close 方法正常工作"""
        mock_sess = AsyncMock()
        mock_sess.closed = False
        client._session = mock_sess

        await client.close()
        mock_sess.close.assert_awaited_once()
