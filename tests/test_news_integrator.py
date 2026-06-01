"""Tests for analysis/news_integrator.py — 新闻情绪融合"""
from __future__ import annotations

import pytest

from analysis.news_integrator import NewsIntegrator, SentimentSignal
from data.sentiment_feed import SentimentReading


class TestNewsIntegrator:
    """新闻情绪融合逻辑测试"""

    @pytest.fixture
    def integrator(self) -> NewsIntegrator:
        return NewsIntegrator()

    @pytest.mark.asyncio
    async def test_no_news_no_fg_returns_base(self, integrator):
        """无新闻无 F&G → 返回原始置信度"""
        signal = await integrator.integrate(base_confidence=0.8)
        assert isinstance(signal, SentimentSignal)
        assert signal.news_count == 0
        assert signal.fg_value is None
        # 无新闻(default=0.5) 无 F&G(default=0.5): 0.5*0.8 + 0.3*0.5 + 0.2*0.5 = 0.65
        assert signal.adjusted_confidence == pytest.approx(0.65, abs=0.01)

    @pytest.mark.asyncio
    async def test_no_news_with_fg(self, integrator):
        """有 F&G 无新闻"""
        fg = SentimentReading(
            metric="fear_greed", value=50, classification="Neutral",
            timestamp=1000, source="test",
        )
        signal = await integrator.integrate(base_confidence=0.8, fear_greed=fg)
        assert signal.fg_value == 50
        assert signal.news_count == 0
        assert not signal.is_extreme

    @pytest.mark.asyncio
    async def test_extreme_fear_override(self, integrator):
        """极端恐惧 → 置信度减半"""
        fg = SentimentReading(
            metric="fear_greed", value=10, classification="Extreme Fear",
            timestamp=1000, source="test",
        )
        signal = await integrator.integrate(base_confidence=0.9, fear_greed=fg)
        assert signal.is_extreme
        assert signal.adjusted_confidence < 0.5

    @pytest.mark.asyncio
    async def test_extreme_greed_override(self, integrator):
        """极端贪婪 → 置信度减半"""
        fg = SentimentReading(
            metric="fear_greed", value=90, classification="Extreme Greed",
            timestamp=1000, source="test",
        )
        signal = await integrator.integrate(base_confidence=0.9, fear_greed=fg)
        assert signal.is_extreme
        assert signal.adjusted_confidence < 0.5

    @pytest.mark.asyncio
    async def test_override_to_flat(self, integrator):
        """极端 + 低置信度 → override_to_flat=True"""
        fg = SentimentReading(
            metric="fear_greed", value=10, classification="Extreme Fear",
            timestamp=1000, source="test",
        )
        signal = await integrator.integrate(base_confidence=0.2, fear_greed=fg)
        assert signal.override_to_flat

    @pytest.mark.asyncio
    async def test_news_sentiment_positive(self, integrator):
        """正面新闻 → 置信度提升"""
        news = _make_news_item(0.5)
        signal = await integrator.integrate(base_confidence=0.7, news_items=[news])
        assert signal.news_count == 1
        assert signal.news_score > 0.5

    @pytest.mark.asyncio
    async def test_news_sentiment_negative(self, integrator):
        """负面新闻 → 置信度降低"""
        news = _make_news_item(-0.5)
        signal = await integrator.integrate(base_confidence=0.7, news_items=[news])
        assert signal.news_count == 1
        # 负面情绪映射后 < 0.5
        assert signal.news_score < 0.5

    @pytest.mark.asyncio
    async def test_news_sentiment_neutral(self, integrator):
        """中性新闻 → 评分 ≈ 0.5"""
        news = _make_news_item(0.0)
        signal = await integrator.integrate(base_confidence=0.7, news_items=[news])
        assert signal.news_score == pytest.approx(0.5, abs=0.01)

    @pytest.mark.asyncio
    async def test_multiple_news_items(self, integrator):
        """多条新闻 → 加权平均"""
        news = [_make_news_item(0.8), _make_news_item(-0.4), _make_news_item(0.2)]
        signal = await integrator.integrate(base_confidence=0.7, news_items=news)
        assert signal.news_count == 3

    @pytest.mark.asyncio
    async def test_no_extreme_override_when_disabled(self):
        """关闭极端覆写 → 不降级"""
        integ = NewsIntegrator(extreme_override=False)
        fg = SentimentReading(
            metric="fear_greed", value=10, classification="Extreme Fear",
            timestamp=1000, source="test",
        )
        signal = await integ.integrate(base_confidence=0.9, fear_greed=fg)
        assert signal.is_extreme
        assert not signal.override_to_flat
        # 即使极端，也未被覆写
        assert signal.adjusted_confidence > 0.5

    @pytest.mark.asyncio
    async def test_integrate_with_all_sources(self, integrator):
        """全数据源融合"""
        fg = SentimentReading(
            metric="fear_greed", value=45, classification="Fear",
            timestamp=1000, source="test",
        )
        news = [_make_news_item(0.3), _make_news_item(0.1)]
        signal = await integrator.integrate(
            base_confidence=0.8, news_items=news, fear_greed=fg,
            symbol="BTC", current_price=50000.0,
        )
        assert signal.news_count == 2
        assert signal.fg_value == 45
        assert not signal.is_extreme
        assert 0.5 <= signal.adjusted_confidence <= 1.0


def _make_news_item(sentiment: float):
    """创建模拟新闻条目"""
    from dataclasses import dataclass

    @dataclass
    class MockNews:
        title: str = "test"
        sentiment_score: float | None = sentiment
        source: str = "test"

    return MockNews()
