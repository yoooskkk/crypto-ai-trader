"""
模块名称: processor.py
所属层级: 制度识别层 (Regime)
输入来源: indicators Stream（含多周期指标计算结果）
输出去向: regime_signal Stream（市场制度 + 置信度）
关键依赖: detector (RuleBasedDetector), hmm_model (HMMDetector)

indicators → 市场制度识别管道。

消费 indicators Stream，使用规则方法或 HMM 识别当前市场制度，
将结果发布到 regime_signal Stream。
"""

from __future__ import annotations

from typing import Any

import structlog

from regime.detector import RuleBasedDetector
from regime.strategy_switcher import evaluate_and_apply as _evaluate_and_apply

logger = structlog.get_logger(__name__)

# ─── 默认检测器（单例）────────────────────────────────────────
_detector = RuleBasedDetector()


async def process_indicators(message: dict[str, Any]) -> dict[str, Any] | None:
    """
    处理单条 indicators Stream 消息。

    从消息中提取指标值，计算 ADX 和 BB 宽度，
    调用 RuleBasedDetector 判断市场制度。

    参数:
        message: indicators Stream 消息
            - symbol, timeframe, ts, close, indicators: {...}

    返回:
        regime_signal Stream 消息，或 None（指标不足时跳过）
    """
    symbol: str = message.get("symbol", "")
    indicators: dict = message.get("indicators", {})
    close: float = message.get("close", 0.0)

    if not indicators:
        logger.warning("指标数据为空，跳过制度识别", symbol=symbol)
        return None

    # ─── 提取 ADX 和 BB 宽度 ──────────────────────────────
    adx = indicators.get("ADX_14", 0.0)
    bb_width = _calc_bb_width(indicators)
    if bb_width is None:
        logger.debug("BB 宽度计算所需数据不足", symbol=symbol)
        bb_width = 0.0

    # ─── 制度检测 ─────────────────────────────────────────
    result = _detector.detect(adx=adx, bb_width=bb_width)

    logger.info(
        "制度识别完成",
        symbol=symbol,
        regime=result.regime.value,
        confidence=result.confidence,
        adx=result.adx,
        bb_width=result.bb_width,
    )

    # ─── 应用制度参数到 risk.yml ──────────────────────────
    try:
        _evaluate_and_apply({
            "symbol": symbol,
            "ts": message.get("ts", 0),
            "regime": result.regime.value,
            "confidence": result.confidence,
        })
    except Exception as exc:
        logger.warning("制度参数应用失败", symbol=symbol, error=str(exc))

    # ─── 构建输出消息 ─────────────────────────────────────
    output: dict[str, Any] = {
        "symbol": symbol,
        "ts": message.get("ts", 0),
        "regime": result.regime.value,
        "confidence": result.confidence,
        "adx": result.adx,
        "bb_width": result.bb_width,
        "close": close,
        "method": "rule_based",
        "indicators_count": len(indicators),
    }

    return output


def _calc_bb_width(indicators: dict[str, Any]) -> float | None:
    """
    从指标字典中计算 Bollinger Band 宽度。

    优先使用标准化字段 BBW_20_2，若无则从 BB 上下轨计算。
    """
    # 标准化字段
    bbw = indicators.get("BBW_20_2")
    if bbw is not None:
        return float(bbw)

    # 回退：从上下轨计算
    bb_upper = indicators.get("BBU_20_2")
    bb_lower = indicators.get("BBL_20_2")
    bb_mid = indicators.get("BBM_20_2")
    if bb_upper is not None and bb_lower is not None and bb_mid and bb_mid != 0:
        return (float(bb_upper) - float(bb_lower)) / float(bb_mid)

    return None


__all__ = ["process_indicators"]
