"""
模块名称: indicator_panel.py
所属层级: CLI 界面 (UI)
输入来源: config/indicators.yml
输出去向: dict — 选中指标的配置字典
关键依赖: pyyaml · rich (推荐)

交互式指标选择面板。
按类别（趋势/动量/波动率/成交量/数学因子/加密Alpha）展示可用指标，
用户可启用/禁用每个组和每个指标，调整参数。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger(__name__)

# ─── 路径 ────────────────────────────────────────

_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "indicators.yml"


# ─── 类别元数据 ──────────────────────────────────

INDICATOR_CATEGORIES: dict[str, dict[str, str]] = {
    "trend": {
        "ema_periods": "指数移动平均 (多个周期)",
        "sma_periods": "简单移动平均 (多个周期)",
        "macd": "MACD 指标 (快线/慢线/信号线)",
        "adx_period": "ADX 趋势强度",
    },
    "momentum": {
        "rsi_period": "RSI 相对强弱指标",
        "roc_period": "ROC 变动率",
        "cci_period": "CCI 商品通道指数",
        "stoch": "随机指标 (K/D/Smooth)",
    },
    "volatility": {
        "atr_period": "ATR 平均真实波幅",
        "stddev_period": "标准差",
        "bbands": "布林带 (周期/标准差)",
    },
    "volume": {
        "mfi_period": "MFI 资金流量指数",
        "cmf_period": "CMF 蔡金资金流",
        "vol_ratio_period": "成交量比率",
    },
    "math_factors": {
        "log_return_period": "对数收益率",
        "zscore_period": "Z-Score 标准化",
        "rank_period": "百分位排名",
        "abs_return_period": "绝对收益率",
    },
    "crypto_alpha": {
        "funding_rate_source": "资金费率 (Binance)",
        "oi_delta_period": "持仓量变化 (24h)",
        "cvd_lookback": "累计成交量差 (CVD)",
    },
}


# ─── 配置模型 ────────────────────────────────────

@dataclass
class IndicatorConfig:
    """选中指标的配置集合。"""
    categories: dict[str, bool] = field(default_factory=dict)  # 类别启用状态
    indicators: dict[str, dict[str, Any]] = field(default_factory=dict)  # 指标参数

    def is_category_enabled(self, name: str) -> bool:
        return self.categories.get(name, True)

    def is_indicator_enabled(self, category: str, indicator: str) -> bool:
        if not self.is_category_enabled(category):
            return False
        cat_inds = self.indicators.get(category, {})
        if indicator in cat_inds:
            return cat_inds[indicator].get("_enabled", True)
        return True


# ─── 加载配置 ────────────────────────────────────

def load_indicator_config() -> dict[str, Any]:
    """从 config/indicators.yml 加载指标配置。"""
    try:
        if not _CONFIG_PATH.exists():
            logger.warning("indicators.yml 未找到", path=str(_CONFIG_PATH))
            return {}
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
            return cfg if isinstance(cfg, dict) else {}
    except Exception as e:
        logger.error("加载指标配置失败", error=str(e))
        return {}


# ─── 渲染面板（文本输出）────────────────────────

def print_indicator_panel(config: IndicatorConfig | None = None) -> None:
    """打印当前指标配置的面板总览。"""
    cfg = config or IndicatorConfig()
    raw = load_indicator_config()

    print("\n" + "=" * 56)
    print("  指标配置面板")
    print("=" * 56)

    for cat_name, indicators in INDICATOR_CATEGORIES.items():
        cat_enabled = cfg.is_category_enabled(cat_name)
        status = "✅" if cat_enabled else "❌"
        cat_label = cat_name.replace("_", " ").title()
        print(f"\n  [{status}] {cat_label}")

        if not cat_enabled:
            continue

        raw_cat = raw.get(cat_name, {})
        for ind_key, desc in indicators.items():
            ind_enabled = cfg.is_indicator_enabled(cat_name, ind_key)
            ind_status = "✔" if ind_enabled else "✘"
            raw_val = raw_cat.get(ind_key, "-")
            # 格式化参数值
            if isinstance(raw_val, dict):
                val_str = ", ".join(f"{k}={v}" for k, v in raw_val.items())
            elif isinstance(raw_val, list):
                val_str = str(raw_val)
            else:
                val_str = str(raw_val)
            print(f"    {ind_status} {ind_key:.<25} {desc:.<20} [{val_str}]")

    print("\n" + "=" * 56)


# ─── 交互式选择 ─────────────────────────────────

def pick_indicators() -> IndicatorConfig:
    """
    交互式选择指标。

    流程:
      1. 显示所有指标类别和指标
      2. 用户可通过编号切换类别/指标的启用状态
      3. 输入 'q' 完成选择并返回配置

    返回:
        IndicatorConfig
    """
    raw = load_indicator_config()
    config = IndicatorConfig()

    # 初始化：所有类别默认启用，指标使用 yml 参数
    for cat_name in INDICATOR_CATEGORIES:
        config.categories[cat_name] = True
        config.indicators[cat_name] = {}
        raw_cat = raw.get(cat_name, {})
        for ind_key in INDICATOR_CATEGORIES[cat_name]:
            if ind_key in raw_cat:
                config.indicators[cat_name][ind_key] = {
                    "_enabled": True,
                    "value": raw_cat[ind_key],
                }
            else:
                config.indicators[cat_name][ind_key] = {
                    "_enabled": False,
                    "value": None,
                }

    while True:
        print_indicator_panel(config)

        print("操作说明:")
        print("  <类别编号>        — 切换类别启用/禁用")
        print("  <类别编号>.<指标号> — 切换单个指标启用/禁用")
        print("  r                 — 重置为默认")
        print("  q                 — 完成并返回")
        print("=" * 56)

        user_input = input("输入: ").strip().lower()

        if user_input in ("q", "quit", "exit"):
            break

        if user_input == "r":
            config = IndicatorConfig()
            for cat_name in INDICATOR_CATEGORIES:
                config.categories[cat_name] = True
                config.indicators[cat_name] = {}
                raw_cat = raw.get(cat_name, {})
                for ind_key in INDICATOR_CATEGORIES[cat_name]:
                    if ind_key in raw_cat:
                        config.indicators[cat_name][ind_key] = {
                            "_enabled": True,
                            "value": raw_cat[ind_key],
                        }
                    else:
                        config.indicators[cat_name][ind_key] = {
                            "_enabled": False,
                            "value": None,
                        }
            continue

        # 解析输入: "3" 或 "3.2"
        parts = user_input.split(".")
        cat_names = list(INDICATOR_CATEGORIES.keys())

        if parts[0].isdigit():
            cat_idx = int(parts[0]) - 1
            if 0 <= cat_idx < len(cat_names):
                cat_name = cat_names[cat_idx]

                if len(parts) == 1:
                    # 切换类别
                    config.categories[cat_name] = not config.categories.get(cat_name, True)
                    logger.info("切换类别状态", category=cat_name, enabled=config.categories[cat_name])

                elif len(parts) == 2 and parts[1].isdigit():
                    # 切换指标
                    ind_keys = list(INDICATOR_CATEGORIES[cat_name].keys())
                    ind_idx = int(parts[1]) - 1
                    if 0 <= ind_idx < len(ind_keys):
                        ind_key = ind_keys[ind_idx]
                        current = config.indicators[cat_name].get(ind_key, {})
                        current["_enabled"] = not current.get("_enabled", True)
                        config.indicators[cat_name][ind_key] = current
                        logger.info(
                            "切换指标状态",
                            category=cat_name,
                            indicator=ind_key,
                            enabled=current["_enabled"],
                        )
                    else:
                        print(f"  无效指标编号: {parts[1]}")
                else:
                    print("  无效输入格式，请使用 <类别编号> 或 <类别编号>.<指标号>")
            else:
                print(f"  无效类别编号: {parts[0]}")
        else:
            print("  无效输入")

    logger.info("指标选择完成", categories=sum(config.categories.values()))
    return config


# ─── 导出配置为 dict ─────────────────────────────

def export_config(config: IndicatorConfig) -> dict[str, Any]:
    """
    将 IndicatorConfig 导出为可写入 indicators.yml 的字典。
    只包含已启用的类别和指标。
    """
    result: dict[str, Any] = {}
    for cat_name, cat_inds in config.indicators.items():
        if not config.is_category_enabled(cat_name):
            continue
        cat_out: dict[str, Any] = {}
        for ind_key, ind_cfg in cat_inds.items():
            if ind_cfg.get("_enabled", True) and ind_cfg.get("value") is not None:
                cat_out[ind_key] = ind_cfg["value"]
        if cat_out:
            result[cat_name] = cat_out
    return result


def main() -> None:
    """CLI 入口点。"""
    config = pick_indicators()
    exported = export_config(config)
    print("\n" + "=" * 56)
    print("  最终指标配置:")
    print("=" * 56)
    for cat, params in exported.items():
        print(f"  [{cat}]")
        for k, v in params.items():
            print(f"    {k}: {v}")
    print("=" * 56)


if __name__ == "__main__":
    main()

