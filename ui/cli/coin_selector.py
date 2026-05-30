"""
模块名称: coin_selector.py
所属层级: CLI 界面 (UI)
关键依赖: data/market_selector, rich/click
说明: 交互式币种选择的 CLI 入口
"""

from __future__ import annotations

import asyncio

import structlog

logger = structlog.get_logger(__name__)


async def run_coin_selector(top_n: int = 50, max_select: int = 10) -> list[str]:
    """
    运行交互式币种选择器。

    参数:
        top_n: 显示的币种数量
        max_select: 最大可选数量

    返回:
        选中的交易对列表，如 ["BTCUSDT", "ETHUSDT"]
    """
    from data.market_selector import MarketSelector

    selector = MarketSelector()
    try:
        selected = await selector.interactive_select(
            top_n=top_n,
            max_select=max_select,
        )
        return [c.symbol for c in selected]
    finally:
        await selector.close()


def main() -> None:
    """CLI 入口点"""
    selected = asyncio.run(run_coin_selector())
    if selected:
        print(f"\n选中的交易对 ({len(selected)}):")
        for s in selected:
            print(f"  {s}")


if __name__ == "__main__":
    main()

