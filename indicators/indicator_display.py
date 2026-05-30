"""
模块名称: indicator_display.py
所属层级: 指标计算层 (Indicators)
输入来源: indicators dict（列名→值的映射）
输出去向: 格式化的显示结构（list[dict] 或文本表格）
关键依赖: dataclasses, re, typing

修订记录:
- v1.0: 初始实现，完整指标信息表 + 格式化输出 + 信号判断
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# ─── 数据结构 ────────────────────────────────────────────────


@dataclass
class IndicatorInfo:
    """单个指标的完整显示信息"""
    key: str                       # 原始列名，如 "RSI_14"
    display_name: str              # 显示名称，如 "RSI(14)"
    category: str                  # 类别: trend/momentum/volatility/volume/timeseries/crypto
    value: float | None            # 当前值
    interpretation: str            # 含义说明
    reference_range: str           # 参考范围，如 "0~100"
    signal: str                    # 信号方向: overbought/oversold/rising/falling/neutral/NaN
    significance: str              # 重要性: primary/secondary/info
    formatted: str                 # 已格式化的文本，如 "RSI(14) = 58.30 中性"


@dataclass
class DisplayResult:
    """格式化结果：按类别分组的指标"""
    by_category: dict[str, list[IndicatorInfo]] = field(default_factory=dict)
    total_count: int = 0
    valid_count: int = 0
    nan_count: int = 0


# ─── 指标元信息注册表 ───────────────────────────────────────
#
# 使用正则匹配来识别不同周期的指标（如 EMA_9、EMA_21 等）
# 匹配优先级：列表顺序 = 匹配优先级（精确匹配优先）

_INDICATOR_REGISTRY: list[tuple[re.Pattern, str, str, str, str, str]] = [
    # (regex_pattern, display_template, category, interpretation, reference_range, significance)

    # ── 趋势类 ──
    (re.compile(r"^EMA_(\d+)$"),
     "EMA({})", "trend",
     "指数移动平均线，衡量价格趋势方向。价格在 EMA 上方为上升趋势。",
     "价格上方/下方", "primary"),

    (re.compile(r"^SMA_(\d+)$"),
     "SMA({})", "trend",
     "简单移动平均线，平滑价格数据识别趋势。",
     "价格上方/下方", "primary"),

    (re.compile(r"^MACD$"),
     "MACD", "trend",
     "指数平滑异同移动平均线 DIF 线。上穿零轴为多头，下穿为空头。",
     "正值/负值", "primary"),

    (re.compile(r"^MACDh$"),
     "MACD-Hist", "trend",
     "MACD 柱状图（DIF - DEA）。柱体放大表示趋势加速。",
     "正值/负值", "primary"),

    (re.compile(r"^MACDs$"),
     "MACD-Signal", "trend",
     "MACD 信号线（DEA）。DIF 上穿为金叉，下穿为死叉。",
     "—", "secondary"),

    (re.compile(r"^ADX_(\d+)$"),
     "ADX({})", "trend",
     "平均趋向指数。>25 表示强趋势，<20 表示弱趋势/震荡。",
     "0~100", "primary"),

    (re.compile(r"^TS_SLOPE$"),
     "TS_SLOPE", "trend",
     "价格趋势斜率。正值上升，负值下降。",
     "正/负/零", "secondary"),

    # ── 动量类 ──
    (re.compile(r"^RSI_(\d+)$"),
     "RSI({})", "momentum",
     "相对强弱指标。>70 超买，<30 超卖，50 为多空分界。",
     "0~100", "primary"),

    (re.compile(r"^ROC_(\d+)$"),
     "ROC({})", "momentum",
     "价格变动率。正值加速上涨，负值加速下跌。",
     "无固定范围", "secondary"),

    (re.compile(r"^CCI_(\d+)$"),
     "CCI({})", "momentum",
     "商品通道指数。>+100 超买，<-100 超卖，0 为均值回归参考。",
     "-300~+300", "primary"),

    (re.compile(r"^STOCH_K_(\d+)_(\d+)$"),
     "STOCH-K({},{})", "momentum",
     "随机指标 %K 线（快速线）。>80 超买，<20 超卖。",
     "0~100", "primary"),

    (re.compile(r"^STOCH_D_(\d+)_(\d+)$"),
     "STOCH-D({},{})", "momentum",
     "随机指标 %D 线（慢速线）。%K 上穿 %D 为买入信号。",
     "0~100", "secondary"),

    # ── 波动率类 ──
    (re.compile(r"^ATR_(\d+)$"),
     "ATR({})", "volatility",
     "平均真实波幅。值越大表示市场波动越剧烈。",
     "与价格相关", "primary"),

    (re.compile(r"^STDDEV_(\d+)$"),
     "STDDEV({})", "volatility",
     "价格标准差。衡量价格离散程度，值大 = 高波动。",
     "与价格相关", "secondary"),

    (re.compile(r"^BBANDS_upper_(\d+)_(\d+)$"),
     "BB-Upper({},{})", "volatility",
     "布林带上轨（均值 + {} 倍标准差）。价格触及上轨可能超买。",
     "价格上方", "primary"),

    (re.compile(r"^BBANDS_mid_(\d+)_(\d+)$"),
     "BB-Mid({},{})", "volatility",
     "布林带中轨（SMA {}）。中轨方向反映趋势。",
     "—", "secondary"),

    (re.compile(r"^BBANDS_lower_(\d+)_(\d+)$"),
     "BB-Lower({},{})", "volatility",
     "布林带下轨（均值 - {} 倍标准差）。价格触及下轨可能超卖。",
     "价格下方", "primary"),

    # ── 成交量类 ──
    (re.compile(r"^OBV$"),
     "OBV", "volume",
     "能量潮指标。上升趋势中 OBV 创新高确认多头。",
     "与价格趋势对比", "primary"),

    (re.compile(r"^VWAP$"),
     "VWAP", "volume",
     "成交量加权平均价。价格在 VWAP 上方为强势，下方为弱势。",
     "价格上方/下方", "primary"),

    (re.compile(r"^MFI_(\d+)$"),
     "MFI({})", "volume",
     "资金流量指标（带成交量的 RSI）。>80 超买，<20 超卖。",
     "0~100", "primary"),

    (re.compile(r"^CMF_(\d+)$"),
     "CMF({})", "volume",
     "Chaikin 资金流量。正值 = 买入压力，负值 = 卖出压力。",
     "-1~+1", "secondary"),

    (re.compile(r"^VOL_RATIO_(\d+)$"),
     "VolRatio({})", "volume",
     "成交量比率 = 当前量 / {} 日均量。>1.5 放量，<0.5 缩量。",
     ">1.5 放量 / <0.5 缩量", "secondary"),

    # ── 时序类 ──
    (re.compile(r"^DELAY_(\d+)$"),
     "DELAY({})", "timeseries",
     "滞后 {} 期的价格值。用于计算其他衍生指标。",
     "与价格相关", "info"),

    (re.compile(r"^DELTA_(\d+)$"),
     "DELTA({})", "timeseries",
     "{} 期价格差值。正值上涨，负值下跌。",
     "正/负/零", "info"),

    (re.compile(r"^TS_MAX_(\d+)$"),
     "TS-MAX({})", "timeseries",
     "{} 期滚动最高价。判断近期压力位。",
     "—", "info"),

    (re.compile(r"^TS_MIN_(\d+)$"),
     "TS-MIN({})", "timeseries",
     "{} 期滚动最低价。判断近期支撑位。",
     "—", "info"),

    (re.compile(r"^TS_RANK_(\d+)$"),
     "TS-RANK({})", "timeseries",
     "{} 期百分位排名。>0.8 高位，<0.2 低位。",
     "0~1", "secondary"),

    (re.compile(r"^TS_ZSCORE_(\d+)$"),
     "TS-ZSCORE({})", "timeseries",
     "{} 期 Z-Score。>2 显著偏高，<-2 显著偏低。",
     "-3~+3", "secondary"),

    (re.compile(r"^CORR_(\d+)$"),
     "CORR({})", "timeseries",
     "价格与成交量的 {} 期相关系数。正值量价齐升。",
     "-1~+1", "info"),

    # ── 加密专属类 ──
    (re.compile(r"^FUNDING_RATE$"),
     "FundingRate", "crypto",
     "当前资金费率。正值多头付费，负值空头付费。绝对值 >0.1% 极端。",
     "-0.1%~+0.1%（正常）", "primary"),

    (re.compile(r"^OI_DELTA_24h$"),
     "OI-Delta(24h)", "crypto",
     "未平仓合约 24h 变化率。正值资金流入，负值资金流出。",
     "百分比变化", "primary"),

    (re.compile(r"^CVD_DELTA_(\d+)$"),
     "CVD-Delta({})", "crypto",
     "{} 期累积成交量差值。正值买方主导，负值卖方主导。",
     "与价格趋势对比", "primary"),
]


# ─── 信号判断函数 ──────────────────────────────────────────


def _interpret_rsi(value: float, period: int) -> str:
    """RSI 信号解释"""
    if value > 70:
        return "超买 ⬇️"
    elif value > 60:
        return "偏强"
    elif value > 40:
        return "中性"
    elif value > 30:
        return "偏弱"
    else:
        return "超卖 ⬆️"


def _interpret_cci(value: float, period: int) -> str:
    """CCI 信号解释"""
    if value > 200:
        return "严重超买"
    elif value > 100:
        return "超买"
    elif value > -100:
        return "中性"
    elif value > -200:
        return "超卖"
    else:
        return "严重超卖"


def _interpret_stoch(value: float, period_k: str, period_d: str) -> str:
    """Stochastic 信号解释"""
    if value > 80:
        return "超买"
    elif value > 60:
        return "偏强"
    elif value > 40:
        return "中性"
    elif value > 20:
        return "偏弱"
    else:
        return "超卖"


def _interpret_mfi(value: float, period: int) -> str:
    """MFI 信号解释"""
    if value > 80:
        return "超买"
    elif value > 60:
        return "资金流入"
    elif value > 40:
        return "中性"
    elif value > 20:
        return "资金流出"
    else:
        return "超卖"


def _interpret_cmf(value: float, period: int) -> str:
    """CMF 信号解释"""
    if value > 0.3:
        return "强买入压力"
    elif value > 0.05:
        return "弱买入压力"
    elif value > -0.05:
        return "中性"
    elif value > -0.3:
        return "弱卖出压力"
    else:
        return "强卖出压力"


def _interpret_zscore(value: float, period: int) -> str:
    """Z-Score 信号解释"""
    if value > 2:
        return "显著偏高"
    elif value > 1:
        return "偏高"
    elif value > -1:
        return "均值附近"
    elif value > -2:
        return "偏低"
    else:
        return "显著偏低"


def _interpret_ts_rank(value: float, period: int) -> str:
    """TS-RANK 信号解释"""
    if value > 0.8:
        return "高位"
    elif value > 0.6:
        return "中高位"
    elif value > 0.4:
        return "中位"
    elif value > 0.2:
        return "中低位"
    else:
        return "低位"


def _interpret_funding_rate(value: float) -> str:
    """资金费率信号解释"""
    abs_val = abs(value)
    if value > 0:
        if abs_val > 0.001:
            return "极高多头拥挤"
        elif abs_val > 0.0005:
            return "多头偏多"
        elif abs_val > 0.0001:
            return "略偏多头"
        else:
            return "中性"
    elif value < 0:
        if abs_val > 0.001:
            return "极高空头拥挤"
        elif abs_val > 0.0005:
            return "空头偏多"
        elif abs_val > 0.0001:
            return "略偏空头"
        else:
            return "中性"
    else:
        return "中性"


def _interpret_oi_delta(value: float) -> str:
    """OI Delta 信号解释"""
    if value > 10:
        return "资金大量流入"
    elif value > 3:
        return "资金流入"
    elif value > -3:
        return "资金平稳"
    elif value > -10:
        return "资金流出"
    else:
        return "资金大量流出"


def _interpret_generic(value: float) -> str:
    """通用信号"""
    if value > 0:
        return "正值"
    elif value < 0:
        return "负值"
    else:
        return "零值"


# ─── 信号分发表（模式 → 解释函数） ─────────────────────────

_SIGNAL_DISPATCH: list[tuple[re.Pattern, Any]] = [
    (re.compile(r"^RSI_(\d+)$"), _interpret_rsi),
    (re.compile(r"^CCI_(\d+)$"), _interpret_cci),
    (re.compile(r"^STOCH_K_(\d+)_(\d+)$"), _interpret_stoch),
    (re.compile(r"^STOCH_D_(\d+)_(\d+)$"), _interpret_stoch),
    (re.compile(r"^MFI_(\d+)$"), _interpret_mfi),
    (re.compile(r"^CMF_(\d+)$"), _interpret_cmf),
    (re.compile(r"^TS_ZSCORE_(\d+)$"), _interpret_zscore),
    (re.compile(r"^TS_RANK_(\d+)$"), _interpret_ts_rank),
    (re.compile(r"^FUNDING_RATE$"), _interpret_funding_rate),
    (re.compile(r"^OI_DELTA_24h$"), _interpret_oi_delta),
]


# ─── 核心函数 ────────────────────────────────────────────────


def _lookup_indicator(key: str) -> tuple[str, str, str, str, str] | None:
    """
    在注册表中查找指标定义。

    返回:
        (display_template, category, interpretation, reference_range, significance)
        未找到返回 None
    """
    for pattern, display_tmpl, category, interpretation, ref_range, significance in _INDICATOR_REGISTRY:
        match = pattern.match(key)
        if match:
            # 用匹配到的组填充 display_template
            groups = match.groups()
            if groups:
                display_name = display_tmpl.format(*groups)
                # 如果 interpretation 包含 {}，也填充
                if "{}" in interpretation:
                    interpretation = interpretation.format(*groups)
            else:
                display_name = display_tmpl
            return (display_name, category, interpretation, ref_range, significance)
    return None


def _get_signal(key: str, value: float) -> str:
    """根据指标名和值返回信号方向。"""
    for pattern, func in _SIGNAL_DISPATCH:
        match = pattern.match(key)
        if match:
            try:
                groups = match.groups()
                if len(groups) == 1:
                    return func(value, int(groups[0]))
                elif len(groups) == 2:
                    return func(value, groups[0], groups[1])
                else:
                    return func(value)
            except Exception:
                return "—"
    # 默认：正/负值
    return _interpret_generic(value)


def format_value(value: float | None, ndigits: int = 2) -> str:
    """格式化数值。None 显示为 'N/A'，浮点数保留指定位数。"""
    if value is None:
        return "N/A"
    if isinstance(value, float):
        if abs(value) < 0.0001 and value != 0:
            return f"{value:.{ndigits}e}"
        return f"{value:.{ndigits}f}"
    return str(value)


def get_indicator_info(key: str, value: float | None) -> IndicatorInfo | None:
    """
    获取单个指标的完整显示信息。

    参数:
        key: 指标列名，如 "RSI_14"
        value: 指标值

    返回:
        IndicatorInfo，未找到注册信息返回 None
    """
    lookup = _lookup_indicator(key)
    if lookup is None:
        return None

    display_name, category, interpretation, ref_range, significance = lookup

    if value is not None and not (isinstance(value, float) and pd_isna(value)):
        signal = _get_signal(key, value)
        formatted = f"{display_name} = {format_value(value)}  {signal}"
    else:
        signal = "NaN"
        formatted = f"{display_name} = N/A"

    return IndicatorInfo(
        key=key,
        display_name=display_name,
        category=category,
        value=value,
        interpretation=interpretation,
        reference_range=ref_range,
        signal=signal,
        significance=significance,
        formatted=formatted,
    )


def pd_isna(value: Any) -> bool:
    """检查是否为 NaN（避免直接引入 pandas 依赖）"""
    try:
        import math
        if isinstance(value, float):
            return math.isnan(value)
    except (ValueError, TypeError):
        pass
    return value is None


def format_indicators(
    indicators: dict[str, float | None],
    sort_by_category: bool = True,
) -> DisplayResult:
    """
    格式化完整的指标字典。

    参数:
        indicators: 列名→值的映射，如 {"RSI_14": 58.3, "ATR_14": 380.2}
        sort_by_category: 是否按类别分组

    返回:
        DisplayResult：包含按类别分组的指标信息
    """
    result = DisplayResult()

    for key, value in indicators.items():
        info = get_indicator_info(key, value)
        if info is None:
            logger.debug("未知指标，跳过显示", key=key)
            continue

        result.total_count += 1
        if value is not None and not pd_isna(value):
            result.valid_count += 1
        else:
            result.nan_count += 1

        category = info.category
        if category not in result.by_category:
            result.by_category[category] = []
        result.by_category[category].append(info)

    # 在每个类别内按 significance 排序
    _sig_order = {"primary": 0, "secondary": 1, "info": 2}
    for cat in result.by_category:
        result.by_category[cat].sort(
            key=lambda x: (_sig_order.get(x.significance, 99), x.key)
        )

    return result


def display_as_text(indicators: dict[str, float | None], include_interpretation: bool = False) -> str:
    """
    将指标格式化为纯文本表格。

    参数:
        indicators: 列名→值的映射
        include_interpretation: 是否包含含义说明

    返回:
        格式化的文本块
    """
    result = format_indicators(indicators)

    lines: list[str] = []
    category_names = {
        "trend": "📈 趋势指标",
        "momentum": "⚡ 动量指标",
        "volatility": "📊 波动率指标",
        "volume": "📦 成交量指标",
        "timeseries": "⏱ 时序指标",
        "crypto": "🔗 加密专属指标",
    }

    for cat in ["trend", "momentum", "volatility", "volume", "timeseries", "crypto"]:
        items = result.by_category.get(cat)
        if not items:
            continue

        cat_name = category_names.get(cat, cat)
        lines.append(f"\n{'='*60}")
        lines.append(f"  {cat_name}  ({len(items)}项)")
        lines.append(f"{'='*60}")

        for item in items:
            # 重要性标记
            sig_mark = "★" if item.significance == "primary" else "·"
            lines.append(f"  {sig_mark} {item.formatted}")
            if include_interpretation and item.significance != "info":
                lines.append(f"       {item.interpretation[:60]}")

    # 统计信息
    lines.append(f"\n{'─'*60}")
    lines.append(f"  有效: {result.valid_count} | NaN: {result.nan_count} | 总计: {result.total_count}")
    lines.append(f"{'─'*60}")

    return "\n".join(lines)


def display_as_json(indicators: dict[str, float | None]) -> dict[str, Any]:
    """
    将指标格式化为 JSON 友好的结构。
    供 CLI 面板或 Web dashboard 调用。

    返回:
        {
            "categories": {
                "trend": [{"key":"RSI_14", "display_name":"RSI(14)", ...}],
                ...
            },
            "summary": {"total": 37, "valid": 30, "nan": 7}
        }
    """
    result = format_indicators(indicators)

    categories = {}
    for cat, items in result.by_category.items():
        categories[cat] = [
            {
                "key": item.key,
                "display_name": item.display_name,
                "value": item.value,
                "signal": item.signal,
                "interpretation": item.interpretation,
                "reference_range": item.reference_range,
                "significance": item.significance,
            }
            for item in items
        ]

    return {
        "categories": categories,
        "summary": {
            "total": result.total_count,
            "valid": result.valid_count,
            "nan": result.nan_count,
        },
    }


def filter_by_significance(
    indicators: dict[str, float | None],
    min_significance: str = "secondary",
) -> dict[str, float | None]:
    """
    过滤指标，只保留指定重要性以上的条目。

    参数:
        indicators: 原始指标字典
        min_significance: "primary"（只看核心）"secondary"（看主要+次要）"info"（全部）

    返回:
        过滤后的指标字典
    """
    levels = {"primary": 0, "secondary": 1, "info": 2}
    min_level = levels.get(min_significance, 1)

    filtered = {}
    for key, value in indicators.items():
        lookup = _lookup_indicator(key)
        if lookup:
            sig_level = levels.get(lookup[4], 99)
            if sig_level <= min_level:
                filtered[key] = value

    return filtered


# ─── 实用性常数 ──────────────────────────────────────────────

# 各类别颜色的 ANSI 代码（供 CLI 使用）
CATEGORY_COLORS: dict[str, str] = {
    "trend": "\033[36m",      # 青色
    "momentum": "\033[33m",   # 黄色
    "volatility": "\033[35m", # 品红
    "volume": "\033[32m",    # 绿色
    "timeseries": "\033[34m", # 蓝色
    "crypto": "\033[31m",    # 红色
}
RESET_COLOR = "\033[0m"


# ─── 公开 API ────────────────────────────────────────────────

__all__ = [
    "IndicatorInfo",
    "DisplayResult",
    "get_indicator_info",
    "format_indicators",
    "display_as_text",
    "display_as_json",
    "filter_by_significance",
    "format_value",
    "CATEGORY_COLORS",
    "RESET_COLOR",
]
