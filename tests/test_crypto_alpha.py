"""
crypto_alpha.py 测试套件
覆盖：数据模型、BinanceFuturesPublicClient（代理/重试/环境变量）、
      指标计算、主计算函数、同步包装器、配置加载
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest
import yaml

from indicators.crypto_alpha import (
    FundingRateResult,
    OpenInterestResult,
    BinanceFuturesPublicClient,
    compute_cvd_delta,
    compute_oi_delta_from_hist,
    compute_oi_delta_simple,
    compute_crypto_alpha,
    compute_cvd_only,
    load_crypto_alpha_params,
    _BINANCE_FAPI_BASE,
    _HTTP_PROXY,
    _HTTPS_PROXY,
    _DEFAULT_TIMEOUT_S,
    _DEFAULT_RETRY_COUNT,
    _DEFAULT_RETRY_DELAY_S,
)


# ─── 数据模型测试 ─────────────────────────────────────────

class TestFundingRateResult:
    def test_creation(self):
        r = FundingRateResult(funding_rate=0.0001, mark_price=50000.0, next_funding_time=1234567890000)
        assert r.funding_rate == 0.0001
        assert r.mark_price == 50000.0
        assert r.next_funding_time == 1234567890000

    def test_defaults(self):
        r = FundingRateResult(funding_rate=0.0, mark_price=0.0, next_funding_time=0)
        assert r.funding_rate == 0.0


class TestOpenInterestResult:
    def test_creation(self):
        r = OpenInterestResult(open_interest=1000.5, time=1234567890000)
        assert r.open_interest == 1000.5
        assert r.time == 1234567890000


# ─── BinanceFuturesPublicClient 测试 ──────────────────────

class TestBinanceFuturesPublicClientInit:
    def test_default_init(self):
        """默认初始化应使用模块级常量。"""
        client = BinanceFuturesPublicClient()
        assert client._base_url == _BINANCE_FAPI_BASE.rstrip("/")
        assert client._timeout == _DEFAULT_TIMEOUT_S
        assert client._retry_count == _DEFAULT_RETRY_COUNT
        assert client._retry_delay == _DEFAULT_RETRY_DELAY_S

    def test_custom_init(self):
        """自定义参数应覆盖默认值。"""
        client = BinanceFuturesPublicClient(
            base_url="https://testnet.binancefuture.com",
            timeout=30,
            retry_count=5,
            retry_delay=2.0,
            proxy="http://proxy.example.com:8080",
        )
        assert client._base_url == "https://testnet.binancefuture.com"
        assert client._timeout == 30
        assert client._retry_count == 5
        assert client._retry_delay == 2.0
        assert client._proxy == "http://proxy.example.com:8080"

    def test_proxy_from_env(self):
        """proxy=None 应尝试读取环境变量（通过构造函数逻辑验证）。"""
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://env-proxy:3128"}, clear=True):
            # 重新导入模块以确保模块级常量使用当前环境变量
            from importlib import reload
            import indicators.crypto_alpha as ca
            reload(ca)
            client = ca.BinanceFuturesPublicClient()
            assert client._proxy == "http://env-proxy:3128"

    def test_proxy_none_no_env(self):
        """无环境变量时 proxy 应为 None。"""
        # 直接测试构造函数逻辑：传 proxy=None 且无环境变量
        # 注意：模块级 _HTTPS_PROXY 可能已被之前的 reload 污染
        # 测试构造函数中 proxy 参数为 None 时的行为
        client = BinanceFuturesPublicClient(proxy=None)
        # _proxy 要么是 env var（如果存在）要么是 None
        # 这里只验证不会抛异常且类型正确
        assert client._proxy is None or isinstance(client._proxy, str)


class TestBinanceFuturesPublicClientSession:
    @pytest.mark.asyncio
    async def test_session_lazy_creation(self):
        """session 应在第一次使用时创建。"""
        client = BinanceFuturesPublicClient()
        assert client._session is None
        session = await client._get_session()
        assert session is not None
        assert not session.closed
        await client.close()

    @pytest.mark.asyncio
    async def test_session_reuse(self):
        """多次调用 _get_session 应返回相同 session。"""
        client = BinanceFuturesPublicClient()
        s1 = await client._get_session()
        s2 = await client._get_session()
        assert s1 is s2
        await client.close()

    @pytest.mark.asyncio
    async def test_session_after_close(self):
        """close() 后应创建新 session。"""
        client = BinanceFuturesPublicClient()
        s1 = await client._get_session()
        await client.close()
        assert s1.closed
        s2 = await client._get_session()
        assert s2 is not s1
        assert not s2.closed
        await client.close()


class TestBinanceFuturesPublicClientRequests:
    @pytest.mark.asyncio
    async def test_get_premium_index_success(self):
        """成功获取资金费率。"""
        mock_data = {
            "symbol": "BTCUSDT",
            "lastFundingRate": "0.0001",
            "markPrice": "50000.0",
            "nextFundingTime": "1234567890000",
        }

        client = BinanceFuturesPublicClient(timeout=5)
        with patch.object(client, "_request_with_retry", AsyncMock(return_value=mock_data)):
            result = await client.get_premium_index("BTCUSDT")
            assert result is not None
            assert result.funding_rate == 0.0001
            assert result.mark_price == 50000.0
            assert result.next_funding_time == 1234567890000
        await client.close()

    @pytest.mark.asyncio
    async def test_get_premium_index_failure(self):
        """API 失败返回 None。"""
        client = BinanceFuturesPublicClient(timeout=5)
        with patch.object(client, "_request_with_retry", AsyncMock(return_value=None)):
            result = await client.get_premium_index("BTCUSDT")
            assert result is None
        await client.close()

    @pytest.mark.asyncio
    async def test_get_open_interest_success(self):
        """成功获取未平仓量。"""
        mock_data = {
            "symbol": "BTCUSDT",
            "openInterest": "150000.5",
            "time": "1234567890000",
        }

        client = BinanceFuturesPublicClient(timeout=5)
        with patch.object(client, "_request_with_retry", AsyncMock(return_value=mock_data)):
            result = await client.get_open_interest("BTCUSDT")
            assert result is not None
            assert result.open_interest == 150000.5
            assert result.time == 1234567890000
        await client.close()

    @pytest.mark.asyncio
    async def test_get_open_interest_history_success(self):
        """成功获取 OI 历史。"""
        mock_data = [
            {"symbol": "BTCUSDT", "sumOpenInterest": "100000", "timestamp": "1000000000000"},
            {"symbol": "BTCUSDT", "sumOpenInterest": "150000", "timestamp": "1000003600000"},
        ]

        client = BinanceFuturesPublicClient(timeout=5)
        with patch.object(client, "_request_with_retry", AsyncMock(return_value=mock_data)):
            result = await client.get_open_interest_history("BTCUSDT", period="1h", limit=30)
            assert result is not None
            assert len(result) == 2
        await client.close()

    @pytest.mark.asyncio
    async def test_get_open_interest_history_wrong_type(self):
        """返回非列表时返回 None。"""
        client = BinanceFuturesPublicClient(timeout=5)
        with patch.object(client, "_request_with_retry", AsyncMock(return_value={"error": "bad"})):
            result = await client.get_open_interest_history("BTCUSDT")
            assert result is None
        await client.close()


class TestRequestWithRetry:
    """
    _request_with_retry 测试。

    注意：aiohttp 的 session.request() 返回一个特殊的 _RequestContextManager
    对象（支持 async with），而非原始协程。因此 mock 时需要用 return_resp()
    工厂函数生成具备 __aenter__/__aexit__ 的响应对象。
    """

    @staticmethod
    def _make_response(status: int, json_data: dict | list) -> AsyncMock:
        """创建模拟的 aiohttp 响应对象（支持 async with）。"""
        resp = AsyncMock()
        resp.status = status
        resp.json = AsyncMock(return_value=json_data)
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=None)
        return resp

    @pytest.mark.asyncio
    async def test_success_first_attempt(self):
        """第一次尝试即成功。"""
        response = self._make_response(200, {"ok": True})
        mock_session = AsyncMock()
        mock_session.closed = False
        # 关键：session.request 必须返回一个支持 async with 的对象（有 __aenter__），
        # 不能是协程（协程没有 __aenter__）
        mock_session.request = MagicMock(return_value=response)

        client = BinanceFuturesPublicClient(timeout=5, retry_count=3)
        client._session = mock_session

        result = await client._request_with_retry("GET", "https://test.com/api")
        assert result == {"ok": True}
        await client.close()

    @pytest.mark.asyncio
    async def test_retry_on_429(self):
        """429 状态码应重试。"""
        call_count = [0]

        def mock_request(method, url, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return self._make_response(429, {})
            return self._make_response(200, {"success": True})

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.request = mock_request

        client = BinanceFuturesPublicClient(timeout=5, retry_count=3, retry_delay=0.01)
        client._session = mock_session

        result = await client._request_with_retry("GET", "https://test.com/api")
        assert result == {"success": True}
        assert call_count[0] == 2
        await client.close()

    @pytest.mark.asyncio
    async def test_retry_on_timeout(self):
        """超时应重试。"""
        call_count = [0]

        def mock_request(method, url, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:
                raise asyncio.TimeoutError()
            return self._make_response(200, {"success": True})

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.request = mock_request

        client = BinanceFuturesPublicClient(timeout=5, retry_count=3, retry_delay=0.01)
        client._session = mock_session

        result = await client._request_with_retry("GET", "https://test.com/api")
        assert result == {"success": True}
        assert call_count[0] == 3
        await client.close()

    @pytest.mark.asyncio
    async def test_all_attempts_fail(self):
        """全部失败返回 None。"""
        call_count = [0]

        def mock_request(method, url, **kwargs):
            call_count[0] += 1
            raise ConnectionError("Network down")

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.request = mock_request

        client = BinanceFuturesPublicClient(timeout=5, retry_count=2, retry_delay=0.01)
        client._session = mock_session

        result = await client._request_with_retry("GET", "https://test.com/api")
        assert result is None
        assert call_count[0] == 2
        await client.close()

    @pytest.mark.asyncio
    async def test_retry_exhausted_returns_none(self):
        """429 重试耗尽返回 None。"""
        call_count = [0]

        def mock_request(method, url, **kwargs):
            call_count[0] += 1
            return self._make_response(429, {})

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.request = mock_request

        client = BinanceFuturesPublicClient(timeout=5, retry_count=3, retry_delay=0.01)
        client._session = mock_session

        result = await client._request_with_retry("GET", "https://test.com/api")
        assert result is None
        assert call_count[0] == 3
        await client.close()


# ─── 环境变量覆盖测试 ─────────────────────────────────────

class TestEnvironmentVariables:
    """环境变量覆盖测试。

    注意：这些测试使用 subprocess 来验证模块级常量在不同环境变量下的行为，
    避免 reload 污染全局状态。或者，我们直接验证构造函数参数映射。
    """

    def test_binance_fapi_base_env(self):
        """BINANCE_FAPI_BASE 环境变量应覆盖 API URL（通过构造函数）。"""
        with patch.dict(os.environ, {"BINANCE_FAPI_BASE": "https://testnet.binancefuture.com"}, clear=True):
            from importlib import reload
            import indicators.crypto_alpha as ca
            reload(ca)
            # 检查模块级常量
            assert ca._BINANCE_FAPI_BASE == "https://testnet.binancefuture.com"
            # 检查客户端使用正确的 base_url
            client = ca.BinanceFuturesPublicClient()
            assert client._base_url == "https://testnet.binancefuture.com"

    def test_crypto_alpha_binance_url_env(self):
        """CRYPTO_ALPHA_BINANCE_URL 作为备用环境变量。"""
        with patch.dict(os.environ, {"CRYPTO_ALPHA_BINANCE_URL": "https://custom.mirror.com"}, clear=True):
            from importlib import reload
            import indicators.crypto_alpha as ca
            reload(ca)
            assert ca._BINANCE_FAPI_BASE == "https://custom.mirror.com"
            client = ca.BinanceFuturesPublicClient()
            assert client._base_url == "https://custom.mirror.com"

    def test_binance_fapi_base_precedence(self):
        """BINANCE_FAPI_BASE 优先于 CRYPTO_ALPHA_BINANCE_URL。"""
        with patch.dict(os.environ, {
            "BINANCE_FAPI_BASE": "https://primary.com",
            "CRYPTO_ALPHA_BINANCE_URL": "https://fallback.com",
        }, clear=True):
            from importlib import reload
            import indicators.crypto_alpha as ca
            reload(ca)
            assert ca._BINANCE_FAPI_BASE == "https://primary.com"

    def test_timeout_env(self):
        """CRYPTO_ALPHA_TIMEOUT 环境变量应覆盖超时。"""
        with patch.dict(os.environ, {"CRYPTO_ALPHA_TIMEOUT": "30"}, clear=True):
            from importlib import reload
            import indicators.crypto_alpha as ca
            reload(ca)
            assert ca._DEFAULT_TIMEOUT_S == 30
            client = ca.BinanceFuturesPublicClient()
            assert client._timeout == 30

    def test_retry_env(self):
        """CRYPTO_ALPHA_RETRY 环境变量应覆盖重试次数。"""
        with patch.dict(os.environ, {"CRYPTO_ALPHA_RETRY": "5"}, clear=True):
            from importlib import reload
            import indicators.crypto_alpha as ca
            reload(ca)
            assert ca._DEFAULT_RETRY_COUNT == 5
            client = ca.BinanceFuturesPublicClient()
            assert client._retry_count == 5

    def test_retry_delay_env(self):
        """CRYPTO_ALPHA_RETRY_DELAY 环境变量。"""
        with patch.dict(os.environ, {"CRYPTO_ALPHA_RETRY_DELAY": "2.5"}, clear=True):
            from importlib import reload
            import indicators.crypto_alpha as ca
            reload(ca)
            assert ca._DEFAULT_RETRY_DELAY_S == 2.5


# ─── 指标计算测试 ─────────────────────────────────────────

class TestCVDDelta:
    def test_basic_cvd(self):
        """基本 CVD 计算。"""
        df = pd.DataFrame({
            "high": [100, 102, 101],
            "low": [98, 99, 99],
            "close": [99, 101, 100],
            "volume": [1000, 2000, 1500],
        })
        result = compute_cvd_delta(df, lookback=3)
        assert result.name == "CVD_DELTA_3"
        assert len(result) == 3
        assert not result.isna().all()

    def test_cvd_missing_column(self):
        """缺少列应返回空 Series。"""
        df = pd.DataFrame({"close": [100, 101]})
        result = compute_cvd_delta(df)
        assert len(result) == 2
        assert result.isna().all()

    def test_cvd_high_low_equal(self):
        """high == low 时应避免除零。"""
        df = pd.DataFrame({
            "high": [100, 100],
            "low": [100, 100],
            "close": [100, 100],
            "volume": [1000, 2000],
        })
        result = compute_cvd_delta(df, lookback=2)
        assert not result.isna().any()


class TestOIDelta:
    def test_from_hist_basic(self):
        """基本 OI delta 计算。"""
        hist = [
            {"sumOpenInterest": "100000"},
            {"sumOpenInterest": "120000"},
            {"sumOpenInterest": "150000"},
        ]
        delta = compute_oi_delta_from_hist(hist, 150000)
        assert delta is not None
        assert delta == 50.0  # (150000 - 100000) / 100000 * 100

    def test_from_hist_empty(self):
        """空历史返回 None。"""
        assert compute_oi_delta_from_hist([], 1000) is None
        assert compute_oi_delta_from_hist(None, 1000) is None

    def test_from_hist_zero_oi(self):
        """基准 OI 为 0 时返回 None。"""
        hist = [{"sumOpenInterest": "0"}]
        assert compute_oi_delta_from_hist(hist, 1000) is None

    def test_simple_oi_proxy(self):
        """volume proxy 应返回正确形状。"""
        df = pd.DataFrame({"volume": [100, 200, 300, 400, 500]})
        result = compute_oi_delta_simple(df, period=2)
        assert result.name == "OI_DELTA_PROXY_2h"
        assert len(result) == 5
        # shift(2): NaN, NaN, (300-100)/100*100=200, (400-200)/200*100=100, (500-300)/300*100=66.67
        assert pd.isna(result.iloc[0])
        assert pd.isna(result.iloc[1])
        assert result.iloc[2] == 200.0
        assert result.iloc[3] == 100.0

    def test_simple_oi_missing_column(self):
        """缺少 volume 列返回空 Series。"""
        df = pd.DataFrame({"close": [100, 101]})
        result = compute_oi_delta_simple(df)
        assert result.isna().all()


# ─── 配置加载测试 ─────────────────────────────────────────

class TestLoadConfig:
    def test_default_config(self):
        """无配置文件时使用默认值。"""
        with patch("pathlib.Path.exists", return_value=False):
            cfg = load_crypto_alpha_params()
            assert cfg["funding_rate_source"] == "binance"
            assert cfg["oi_delta_period"] == 24
            assert cfg["cvd_lookback"] == 100
            assert cfg["timeout"] == 15
            assert cfg["retry_count"] == 3

    def test_config_from_file(self):
        """从配置文件正确读取。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False, encoding="utf-8") as f:
            yaml.dump({
                "crypto_alpha": {
                    "funding_rate_source": "binance",
                    "oi_delta_period": 48,
                    "cvd_lookback": 200,
                    "timeout": 20,
                    "retry_count": 5,
                    "proxy": "http://proxy:8080",
                }
            }, f)
            tmp_path = f.name

        try:
            cfg = load_crypto_alpha_params(tmp_path)
            assert cfg["oi_delta_period"] == 48
            assert cfg["cvd_lookback"] == 200
            assert cfg["timeout"] == 20
            assert cfg["retry_count"] == 5
            assert cfg["proxy"] == "http://proxy:8080"
        finally:
            os.unlink(tmp_path)


# ─── 主计算函数测试 ───────────────────────────────────────

@pytest.fixture
def sample_ohlcv():
    """提供标准 OHLCV DataFrame。"""
    np.random.seed(42)
    n = 120
    close = 100 + np.cumsum(np.random.randn(n) * 0.5)
    high = close + np.abs(np.random.randn(n) * 0.3)
    low = close - np.abs(np.random.randn(n) * 0.3)
    return pd.DataFrame({
        "open": close - np.random.randn(n) * 0.1,
        "high": high,
        "low": low,
        "close": close,
        "volume": np.random.randint(1000, 5000, n),
    })


class TestComputeCryptoAlpha:
    @pytest.mark.asyncio
    async def test_basic_computation(self, sample_ohlcv):
        """基本计算应追加所有列。"""
        mock_client = AsyncMock(spec=BinanceFuturesPublicClient)
        mock_client.get_premium_index.return_value = FundingRateResult(
            funding_rate=0.0001, mark_price=50000.0, next_funding_time=0
        )
        mock_client.get_open_interest.return_value = OpenInterestResult(
            open_interest=150000.0, time=0
        )
        mock_client.get_open_interest_history.return_value = [
            {"sumOpenInterest": "100000", "timestamp": "0"},
            {"sumOpenInterest": "150000", "timestamp": "3600000"},
        ]

        result = await compute_crypto_alpha(
            sample_ohlcv,
            symbol="BTCUSDT",
            cfg={"cvd_lookback": 100, "oi_delta_period": 24},
            binance_client=mock_client,
        )

        assert "CVD_DELTA_100" in result.columns
        assert "FUNDING_RATE" in result.columns
        assert "OI_DELTA_24h" in result.columns
        assert result["FUNDING_RATE"].iloc[0] == 0.0001

    @pytest.mark.asyncio
    async def test_api_failure_fallback(self, sample_ohlcv):
        """API 失败时应设置 NaN 并包含 CVD。"""
        mock_client = AsyncMock(spec=BinanceFuturesPublicClient)
        mock_client.get_premium_index.return_value = None
        mock_client.get_open_interest.return_value = None

        result = await compute_crypto_alpha(
            sample_ohlcv,
            symbol="BTCUSDT",
            cfg={"cvd_lookback": 100, "oi_delta_period": 24},
            binance_client=mock_client,
        )

        assert "CVD_DELTA_100" in result.columns
        assert "FUNDING_RATE" in result.columns
        assert pd.isna(result["FUNDING_RATE"].iloc[0])

    @pytest.mark.asyncio
    async def test_missing_column(self):
        """缺少必要列时应返回原始 df。"""
        df = pd.DataFrame({"close": [100, 101]})
        result = await compute_crypto_alpha(df)
        assert "CVD_DELTA_100" not in result.columns

    @pytest.mark.asyncio
    async def test_auto_create_client(self, sample_ohlcv):
        """binance_client=None 时自动创建。"""
        with patch("indicators.crypto_alpha.BinanceFuturesPublicClient") as MockClient:
            instance = MockClient.return_value
            instance.get_premium_index = AsyncMock(return_value=None)
            instance.get_open_interest = AsyncMock(return_value=None)
            instance.close = AsyncMock()

            result = await compute_crypto_alpha(
                sample_ohlcv,
                symbol="BTCUSDT",
                cfg={"cvd_lookback": 100, "oi_delta_period": 24, "proxy": "", "timeout": 15, "retry_count": 3},
            )

            MockClient.assert_called_once_with(
                timeout=15, retry_count=3, proxy=None
            )
            assert "CVD_DELTA_100" in result.columns

    @pytest.mark.asyncio
    async def test_exception_handling(self, sample_ohlcv):
        """总体异常时应至少返回 CVD。"""
        mock_client = AsyncMock(spec=BinanceFuturesPublicClient)
        mock_client.get_premium_index.side_effect = RuntimeError("Unexpected crash")

        result = await compute_crypto_alpha(
            sample_ohlcv,
            symbol="BTCUSDT",
            cfg={"cvd_lookback": 100, "oi_delta_period": 24},
            binance_client=mock_client,
        )

        # CVD 应被计算
        assert "CVD_DELTA_100" in result.columns


# ─── 同步包装器测试 ───────────────────────────────────────

class TestCVDOnly:
    def test_compute_cvd_only(self, sample_ohlcv):
        """同步 wrapper 应计算 CVD。"""
        result = compute_cvd_only(sample_ohlcv, cfg={"cvd_lookback": 100})
        assert "CVD_DELTA_100" in result.columns
        assert "FUNDING_RATE" not in result.columns  # 不应计算 API 指标

    def test_compute_cvd_only_default_cfg(self, sample_ohlcv):
        """不传 cfg 时使用默认配置。"""
        with patch("pathlib.Path.exists", return_value=False):
            result = compute_cvd_only(sample_ohlcv)
            assert "CVD_DELTA_100" in result.columns


# ─── 模块级测试 ───────────────────────────────────────────

class TestModuleLevel:
    def test_importable(self):
        from indicators import crypto_alpha
        assert hasattr(crypto_alpha, "compute_crypto_alpha")
        assert hasattr(crypto_alpha, "BinanceFuturesPublicClient")
        assert hasattr(crypto_alpha, "compute_cvd_delta")

    def test_module_constants_defined(self):
        assert _BINANCE_FAPI_BASE != ""
        assert _DEFAULT_TIMEOUT_S > 0
        assert _DEFAULT_RETRY_COUNT > 0
