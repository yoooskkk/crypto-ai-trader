"""
模块名称: timeframe_picker.py
所属层级: CLI 界面 (UI)
关键依赖: config/timeframes.yml
说明: 交互式时间周期选择器
"""

from __future__ import annotations

from pathlib import Path

import structlog
import yaml

logger = structlog.get_logger(__name__)


_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "timeframes.yml"


def load_available_timeframes() -> list[str]:
    """从 config/timeframes.yml 加载可用周期"""
    try:
        if not _CONFIG_PATH.exists():
            logger.warning("timeframes.yml 未找到，使用默认", path=str(_CONFIG_PATH))
            return ["1m", "5m", "15m", "1h", "4h", "1d"]

        with open(_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)

        available = cfg.get("available", [])
        if available:
            return available

        logger.warning("配置中无可用的周期列表，使用默认")
        return ["1m", "5m", "15m", "1h", "4h", "1d"]

    except Exception as e:
        logger.error("加载周期配置失败", error=str(e))
        return ["1m", "5m", "15m", "1h", "4h", "1d"]


def pick_timeframe() -> str:
    """
    交互式选择时间周期。

    显示可用周期列表，用户输入编号选择。

    返回:
        选择的周期字符串，如 "1h"
    """
    timeframes = load_available_timeframes()

    print("\n" + "=" * 40)
    print("  选择时间周期")
    print("=" * 40)
    for i, tf in enumerate(timeframes, start=1):
        print(f"  {i}. {tf}")
    print("=" * 40)

    while True:
        user_input = input("输入编号 (1-{})，默认 1h: ".format(len(timeframes))).strip()

        if not user_input:
            return "1h"

        if user_input.lower() in ("q", "quit", "exit"):
            return ""

        if user_input.isdigit():
            idx = int(user_input)
            if 1 <= idx <= len(timeframes):
                selected = timeframes[idx - 1]
                logger.info("选择了周期", timeframe=selected)
                return selected

        print(f"无效输入，请输入 1-{len(timeframes)}")


def pick_timeframes_multi() -> list[str]:
    """
    交互式选择多个时间周期。

    返回:
        选中的周期列表，如 ["1h", "4h", "1d"]
    """
    timeframes = load_available_timeframes()
    selected: list[str] = []

    print("\n" + "=" * 40)
    print("  选择时间周期（多选，逗号分隔）")
    print("=" * 40)
    for i, tf in enumerate(timeframes, start=1):
        print(f"  {i}. {tf}")
    print("=" * 40)

    while True:
        user_input = input("输入编号 (如 1,3,5; q 完成): ").strip()

        if not user_input:
            continue

        if user_input.lower() in ("q", "quit", "exit"):
            break

        parts = [p.strip() for p in user_input.split(",")]
        if all(p.isdigit() for p in parts):
            for p in parts:
                idx = int(p)
                if 1 <= idx <= len(timeframes):
                    tf = timeframes[idx - 1]
                    if tf not in selected:
                        selected.append(tf)
                else:
                    print(f"  无效编号: {idx}")
        else:
            print("请输入逗号分隔的编号")

    logger.info("选择了多周期", timeframes=selected)
    return selected


def main() -> None:
    """CLI 入口点"""
    selected = pick_timeframe()
    if selected:
        print(f"\n选择的周期: {selected}")


if __name__ == "__main__":
    main()

