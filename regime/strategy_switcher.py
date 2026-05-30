"""
模块名称: strategy_switcher.py
所属层级: 制度识别层 (Regime)
输入来源: regime_signal Stream（由 detector.py 或 hmm_model.py 产生）
输出去向: 计算风险参数覆盖值 → 更新 config/risk.yml + 发布配置变更事件
关键依赖: structlog, yaml, pathlib

修订记录:
- v1.0: 初始实现，制度→风险参数映射 + 覆盖值计算 + risk.yml 更新
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger(__name__)


class Regime(str, Enum):
    """市场制度枚举（与 detector.py 对齐）"""
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    UNKNOWN = "UNKNOWN"


# ─── 覆盖值数据结构 ───────────────────────────────────────────

@dataclass
class RegimeOverrides:
    """
    某个制度下的风险参数覆盖值。
    所有字段均有默认值，未覆盖的参数保持风险配置原值。
    """
    # 仓位控制
    max_total_pct: float | None = None           # 总仓位上限（%)
    max_single_position_pct: float | None = None # 单币仓位上限（%)
    position_size_multiplier: float | None = None  # 仓位系数（乘数，默认1.0）

    # 风控门槛
    min_confidence: float | None = None           # 最低信号置信度
    stop_loss_multiplier: float | None = None     # 止损倍数（相对于正常止损，<1 更紧）

    # 策略偏好（影响 ai_engine/plan_generator.py 和 signal_scorer.py）
    preferred_indicators: list[str] | None = None  # 优先使用的指标类型
    disable_trend_signals: bool | None = None      # 是否关闭趋势信号（仅 RANGING 启用）

    # 冷却控制
    cooldown_hours: int | None = None             # 熔断后冷却时间


@dataclass
class StrategySwitchResult:
    """制度切换结果：包含原始信号 + 计算出的覆盖值"""
    symbol: str
    ts: int
    regime: Regime
    confidence: float
    prev_regime: Regime | None
    overrides: RegimeOverrides
    is_switch: bool  # 是否发生了制度切换


# ─── 制度→策略映射表（对应 ARCH.md 第7节） ───────────────────

# 默认参数（来自 config/risk.yml 的基线值）
_DEFAULT_PARAMS: dict[str, Any] = {
    "max_total_pct": 80.0,
    "min_confidence": 0.65,
    "position_size_multiplier": 1.0,
    "stop_loss_multiplier": 1.0,
    "max_single_position_pct": 20.0,
    "cooldown_hours": 4,
}

_REGIME_STRATEGY_MAP: dict[Regime, RegimeOverrides] = {
    # TRENDING → 满仓操作，趋势指标优先
    Regime.TRENDING: RegimeOverrides(
        max_total_pct=80.0,
        max_single_position_pct=25.0,
        position_size_multiplier=1.0,
        min_confidence=0.65,
        stop_loss_multiplier=1.0,
        preferred_indicators=["ema", "macd", "adx"],
        disable_trend_signals=False,
        cooldown_hours=2,  # 趋势中冷却时间缩短
    ),

    # RANGING → 半仓，震荡指标优先，关闭趋势信号避免假突破
    Regime.RANGING: RegimeOverrides(
        max_total_pct=40.0,
        max_single_position_pct=15.0,
        position_size_multiplier=0.6,
        min_confidence=0.70,
        stop_loss_multiplier=1.0,
        preferred_indicators=["rsi", "stoch","cci"],
        disable_trend_signals=True,
        cooldown_hours=4,
    ),

    # HIGH_VOLATILITY → 半仓系数 + 收窄止损
    Regime.HIGH_VOLATILITY: RegimeOverrides(
        max_total_pct=40.0,
        max_single_position_pct=12.0,
        position_size_multiplier=0.5,
        min_confidence=0.75,
        stop_loss_multiplier=0.7,  # 止损收窄 30%
        preferred_indicators=["atr","bbands"],
        disable_trend_signals=False,
        cooldown_hours=6,  # 高波动后需更久冷却
    ),

    # UNKNOWN → 极保守，只有高置信度信号才通过
    Regime.UNKNOWN: RegimeOverrides(
        max_total_pct=20.0,
        max_single_position_pct=8.0,
        position_size_multiplier=0.3,
        min_confidence=0.80,
        stop_loss_multiplier=1.0,
        preferred_indicators=[],
        disable_trend_signals=True,
        cooldown_hours=8,
    ),
}


# ─── 配置路径 ────────────────────────────────────────────────

_CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", Path(__file__).parent.parent / "config"))
RISK_CONFIG_PATH = _CONFIG_DIR / "risk.yml"


# ─── 核心类 ───────────────────────────────────────────────────

class StrategySwitcher:
    """
    制度策略切换器。

    职责:
    1. 接收 regime_signal，判断是否发生制度切换
    2. 根据新制度查表算出风险参数覆盖值
    3. 提供 apply_to_config() 更新 risk.yml
    4. 提供 get_overrides() 供其他模块查询当前有效参数

    用法:
        switcher = StrategySwitcher()
        result = switcher.evaluate(signal_dict)
        if result.is_switch:
            switcher.apply_to_config(result.overrides)
    """

    # class-level 缓存当前制度状态（跨调用保持）
    _current_regimes: dict[str, Regime] = {}
    _current_overrides: dict[str, RegimeOverrides] = {}

    def __init__(
        self,
        risk_config_path: str | Path | None = None,
        strategy_map: dict[Regime, RegimeOverrides] | None = None,
    ):
        """
        初始化策略切换器。

        参数:
            risk_config_path: risk.yml 路径，默认使用 config/risk.yml
            strategy_map: 自定义策略映射表，默认使用内建映射
        """
        self._risk_config_path = Path(risk_config_path) if risk_config_path else RISK_CONFIG_PATH
        self._strategy_map = strategy_map or _REGIME_STRATEGY_MAP

    def evaluate(self, signal: dict[str, Any]) -> StrategySwitchResult:
        """
        评估制度信号，计算参数覆盖值，检测制度切换。

        参数:
            signal: regime_signal Stream 消息，需包含:
                - symbol: str
                - ts: int
                - regime: str（枚举: TRENDING/RANGING/HIGH_VOLATILITY/UNKNOWN）
                - confidence: float
                - prev_regime: str | None（可选）

        返回:
            StrategySwitchResult
        """
        symbol = signal.get("symbol", "UNKNOWN")
        ts = signal.get("ts", 0)
        regime_str = signal.get("regime", "UNKNOWN")
        confidence = signal.get("confidence", 0.0)
        prev_regime_str = signal.get("prev_regime")

        try:
            new_regime = Regime(regime_str.upper())
        except ValueError:
            logger.warning("未知制度", regime=regime_str, symbol=symbol)
            new_regime = Regime.UNKNOWN

        prev_regime = None
        if prev_regime_str:
            try:
                prev_regime = Regime(prev_regime_str.upper())
            except ValueError:
                pass

        # 如果没传 prev_regime，用缓存中的上一次状态
        if prev_regime is None and symbol in self._current_regimes:
            prev_regime = self._current_regimes[symbol]

        is_switch = (prev_regime is not None and prev_regime != new_regime)

        # 查表获取覆盖值
        overrides = self._strategy_map.get(new_regime, _REGIME_STRATEGY_MAP[Regime.UNKNOWN])

        # 缓存当前状态
        self._current_regimes[symbol] = new_regime
        self._current_overrides[symbol] = overrides

        result = StrategySwitchResult(
            symbol=symbol,
            ts=ts,
            regime=new_regime,
            confidence=confidence,
            prev_regime=prev_regime,
            overrides=overrides,
            is_switch=is_switch,
        )

        if is_switch:
            logger.info(
                "制度切换",
                symbol=symbol,
                prev=prev_regime.value if prev_regime else None,
                new=new_regime.value,
                confidence=confidence,
            )
        else:
            logger.debug(
                "制度未变",
                symbol=symbol,
                regime=new_regime.value,
            )

        return result

    def apply_to_config(
        self,
        overrides: RegimeOverrides,
        config_path: str | Path | None = None,
    ) -> bool:
        """
        将制度覆盖值写入 risk.yml 文件。

        策略:
        - 读取当前 risk.yml
        - 将覆盖值中非 None 的字段写入对应位置
        - None 字段保留原值不动
        - 写入前先备份原文件

        参数:
            overrides: 要应用的制度覆盖值
            config_path: 可选，指定写入的文件路径

        返回:
            True 表示写入成功，False 表示失败
        """
        cfg_path = Path(config_path) if config_path else self._risk_config_path

        if not cfg_path.exists():
            logger.error("risk.yml 不存在", path=str(cfg_path))
            return False

        try:
            with open(cfg_path, encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        except Exception as e:
            logger.error("读取 risk.yml 失败", error=str(e))
            return False

        # 备份原文件
        backup_path = cfg_path.with_suffix(".yml.bak")
        try:
            with open(backup_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f, default_flow_style=False)
        except Exception as e:
            logger.warning("备份 risk.yml 失败", error=str(e))

        # 应用覆盖值
        overrides_dict = asdict(overrides)
        changed = []

        # 覆盖 exposure.max_total_pct
        if overrides.max_total_pct is not None:
            old_val = config.get("exposure", {}).get("max_total_pct")
            config.setdefault("exposure", {})["max_total_pct"] = overrides.max_total_pct
            changed.append(f"exposure.max_total_pct: {old_val} -> {overrides.max_total_pct}")

        # 覆盖 exposure.max_single_position_pct
        if overrides.max_single_position_pct is not None:
            old_val = config.get("exposure", {}).get("max_single_position_pct")
            config.setdefault("exposure", {})["max_single_position_pct"] = overrides.max_single_position_pct
            changed.append(f"exposure.max_single_position_pct: {old_val} -> {overrides.max_single_position_pct}")

        # 覆盖 signal.min_confidence
        if overrides.min_confidence is not None:
            old_val = config.get("signal", {}).get("min_confidence")
            config.setdefault("signal", {})["min_confidence"] = overrides.min_confidence
            changed.append(f"signal.min_confidence: {old_val} -> {overrides.min_confidence}")

        # 覆盖 circuit_breaker.cool_down_hours
        if overrides.cooldown_hours is not None:
            old_val = config.get("circuit_breaker", {}).get("cool_down_hours")
            config.setdefault("circuit_breaker", {})["cool_down_hours"] = overrides.cooldown_hours
            changed.append(f"circuit_breaker.cool_down_hours: {old_val} -> {overrides.cooldown_hours}")

        # 写入新配置
        try:
            with open(cfg_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f, default_flow_style=False)
            logger.info("risk.yml 已更新", changes=changed)
            return True
        except Exception as e:
            logger.error("写入 risk.yml 失败", error=str(e))
            # 尝试恢复备份
            if backup_path.exists():
                import shutil
                shutil.copy2(str(backup_path), str(cfg_path))
                logger.info("已从备份恢复 risk.yml")
            return False

    def get_current_regime(self, symbol: str = "DEFAULT") -> Regime | None:
        """获取指定交易对的当前制度。"""
        return self._current_regimes.get(symbol)

    def get_current_overrides(self, symbol: str = "DEFAULT") -> RegimeOverrides | None:
        """获取指定交易对的当前覆盖值。"""
        return self._current_overrides.get(symbol)

    def get_effective_params(self, symbol: str = "DEFAULT") -> dict[str, Any]:
        """
        获取当前生效的风险参数（合并基线 + 覆盖值）。
        供其他模块查询当前该用什么参数。
        """
        params = dict(_DEFAULT_PARAMS)
        overrides = self.get_current_overrides(symbol)
        if overrides is not None:
            override_dict = asdict(overrides)
            for key, val in override_dict.items():
                if val is not None:
                    # 将 dataclass 字段名映射回 risk.yml 兼容的键名
                    params[key] = val
        return params

    def reset(self, symbol: str | None = None) -> None:
        """重置缓存状态（用于测试或强制刷新）。"""
        if symbol:
            self._current_regimes.pop(symbol, None)
            self._current_overrides.pop(symbol, None)
        else:
            self._current_regimes.clear()
            self._current_overrides.clear()
        logger.info("策略切换器状态已重置", symbol=symbol or "ALL")


# ─── 便捷函数（供上层 worker 直接调用） ─────────────────────

def create_default_switcher() -> StrategySwitcher:
    """创建使用默认配置的策略切换器。"""
    return StrategySwitcher()


def evaluate_and_apply(signal: dict[str, Any], switcher: StrategySwitcher | None = None) -> StrategySwitchResult:
    """
    便捷函数：一步完成评估 + 应用。

    参数:
        signal: regime_signal 消息
        switcher: 复用已有的切换器，为 None 时创建默认的

    返回:
        StrategySwitchResult
    """
    if switcher is None:
        switcher = create_default_switcher()

    result = switcher.evaluate(signal)
    if result.is_switch:
        switcher.apply_to_config(result.overrides)

    return result
