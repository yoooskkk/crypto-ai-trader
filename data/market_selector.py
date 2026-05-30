"""
模块名称: market_selector.py
所属层级: 数据采集层 (Data)
输入来源: Binance REST API（通过 rest_client）
输出去向: 返回选中的交易对列表给其他模块 / 交互式 CLI 选择
关键依赖: data/rest_client, structlog

功能说明:
    获取 Binance 前 N 个活跃交易对（按 quote_volume 排序），
    支持编程式获取和交互式 CLI 选择两种模式。

注意:
    CLI 交互模式使用 print() / input() 是合理的（与用户终端交互），
    但不使用 print() 记录日志 —— 所有日志走 structlog。
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class CoinInfo:
    """币种信息"""
    symbol: str
    base_asset: str
    quote_asset: str
    last_price: float
    volume_24h: float
    quote_volume_24h: float
    price_change_pct: float
    rank: int = 0


class MarketSelector:
    """Binance 市场选择器，支持编程模式和交互模式"""

    def __init__(self, rest_client=None):
        self._rest = rest_client
        self._own_client = False

    async def _get_rest(self):
        if self._rest is None:
            from data.rest_client import BinancePublicClient
            self._rest = BinancePublicClient()
            self._own_client = True
        return self._rest

    async def close(self) -> None:
        if self._own_client and self._rest:
            await self._rest.close()

    async def get_top_symbols(
        self,
        top_n: int = 50,
        quote_asset: str = "USDT",
        min_volume: float = 1_000_000,
    ) -> list[CoinInfo]:
        """获取前 N 个交易对（按 quote_volume 降序）"""
        rest = await self._get_rest()
        tickers = await rest.get_top_symbols(
            quote_asset=quote_asset,
            top_n=top_n * 2,
            min_volume=min_volume,
        )
        if not tickers:
            return []
        coins = []
        for rank, t in enumerate(tickers, start=1):
            try:
                coins.append(CoinInfo(
                    symbol=t.symbol,
                    base_asset=t.symbol.replace(quote_asset, ""),
                    quote_asset=quote_asset,
                    last_price=float(t.last_price),
                    volume_24h=float(t.volume),
                    quote_volume_24h=float(t.quote_volume),
                    price_change_pct=float(t.price_change_pct),
                    rank=rank,
                ))
            except (TypeError, ValueError) as e:
                logger.warning("解析 CoinInfo 失败", symbol=t.symbol, error=str(e))
                continue
        return coins[:top_n]

    async def search_symbol(self, query: str, top_n: int = 50) -> list[CoinInfo]:
        coins = await self.get_top_symbols(top_n=top_n)
        if not coins:
            return []
        query = query.upper()
        return [c for c in coins if query in c.symbol or query in c.base_asset]

    async def interactive_select(
        self,
        top_n: int = 50,
        prompt: str = "选择交易对(输入编号/名称，q 完成): ",
        max_select: int = 10,
    ) -> list[CoinInfo]:
        """
        交互式 CLI 选择币种。
        直接使用 print()/input() 与终端交互，不使用日志系统输出界面。
        """
        coins = await self.get_top_symbols(top_n=top_n)
        if not coins:
            return []
        selected: list[CoinInfo] = []
        while True:
            self._display_coins(coins, selected)
            user_input = input(f"\n{prompt}").strip()
            if not user_input:
                continue
            if user_input.lower() in ("q", "quit", "exit"):
                break
            if all(p.strip().isdigit() for p in user_input.split(",")):
                for idx in [int(p.strip()) for p in user_input.split(",")]:
                    if 1 <= idx <= len(coins):
                        coin = coins[idx - 1]
                        if coin not in selected:
                            selected.append(coin)
                            if len(selected) >= max_select:
                                return selected
                    else:
                        print(f"  无效编号: {idx}")
            else:
                results = [c for c in coins if user_input.upper() in c.symbol or user_input.upper() in c.base_asset]
                if results:
                    print(f"\n  找到 {len(results)} 个匹配项:")
                    for r in results:
                        m = " ✓" if r in selected else ""
                        print(f"    {r.symbol:12s}  ${r.last_price:<10.2f}  ${r.quote_volume_24h:>12,.0f}{m}")
                    if input("  添加全部？(y/n): ").strip().lower() == "y":
                        for r in results:
                            if r not in selected:
                                selected.append(r)
                                if len(selected) >= max_select:
                                    return selected
                else:
                    print(f"  未找到匹配: {user_input}")
        return selected

    @staticmethod
    def _display_coins(coins: list[CoinInfo], selected: list[CoinInfo]) -> None:
        """显示币种列表（终端交互用，不走日志）"""
        selected_symbols = {c.symbol for c in selected}
        print("\n" + "=" * 80)
        print(f"{'#':>4}  {'交易对':<12} {'价格':<14} {'24h涨跌':<10} {'24h成交额(USDT)':<18}")
        print("-" * 80)
        for i, coin in enumerate(coins, start=1):
            m = " ✓" if coin.symbol in selected_symbols else "  "
            print(
                f"{i:>4}. {coin.symbol:<12} "
                f"${coin.last_price:<10.2f} "
                f"{coin.price_change_pct:>+7.2f}% "
                f"${coin.quote_volume_24h:>12,.0f}"
                f"{m}"
            )
        print("=" * 80)
        if selected:
            print(f"已选: {', '.join(c.symbol for c in selected)}")
