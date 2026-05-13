"""
AiSignalStrategy — Freqtrade 主策略
从 Redis 读取 AI 引擎生成的信号，经风险控制层仲裁后执行
"""
from freqtrade.strategy import IStrategy
import pandas as pd


class AiSignalStrategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    minimal_roi = {"0": 0.05}
    stoploss = -0.03
    trailing_stop = True

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        # 指标由独立 worker 预计算，此处直接读取缓存
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        # TODO: 从 Redis 读取 AI 信号
        dataframe["enter_long"]  = 0
        dataframe["enter_short"] = 0
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["exit_long"]  = 0
        dataframe["exit_short"] = 0
        return dataframe
