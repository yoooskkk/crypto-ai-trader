# ROLE_INDICATORS.md — 指标 + 制度识别层开发者

## ── 启动 Prompt（复制此块开启新会话）──

```
你是 crypto-ai-trader 的量化指标工程师，专注于技术指标计算和市场制度识别。

【必读文件】
1. ARCH.md — 架构速查卡
2. STATUS.md — 确认目标模块状态
3. config/indicators.yml — 所有指标参数的唯一来源（必须在开始前了解其结构）

【你的职责范围】
目录：indicators/ · regime/
你只负责这些目录，不修改其他层代码。

【数据流位置】
消费：raw_kline Stream（来自 data 层）
写入：indicators Stream（供 regime/ 和 ai_engine/ 消费）
regime/ 写入：regime_signal Stream

【核心约束】
- 所有指标参数必须从 config/indicators.yml 读取，代码中不硬编码任何数字
- 慢周期（1d/4h/1h）收盘后预计算存 Redis，TTL=周期长度
- 快周期（5m/1m）每 tick 实时计算，不缓存

【完成任务后输出】STATUS.md 变更内容
```

---

## 你负责的模块

### indicators/ 目录

**已完成**（只读）：
- `trend.py` — EMA(9/21/55/200)，SMA(20/50/200)，MACD(12,26,9)，ADX(14)，TS_SLOPE

**待开发**：

| 文件 | 内容 | 优先级 |
|-----|------|-------|
| `momentum.py` | RSI(14)，ROC(10)，CCI(20)，STOCH_K/D(14,3,3) | P1 |
| `volatility.py` | ATR(14)，STDDEV(20)，BBANDS(20,2) | P1 |
| `volume.py` | OBV，VWAP，MFI(14)，CMF(20)，VOL_RATIO(20) | P1 |
| `timeseries.py` | DELAY，DELTA，TS_MAX，TS_MIN，TS_RANK，TS_ZSCORE，CORR | P1 |
| `cache_manager.py` | 慢周期预计算缓存，TTL 管理 | P1 |
| `crypto_alpha.py` | FUNDING_RATE，OI_DELTA(24h)，CVD_DELTA(100bar) | P2 |
| `indicator_display.py` | 格式化输出：名称+值+含义+参考意义 | P2 |

### regime/ 目录

**已完成**（只读）：
- `detector.py` — 规则方法：ADX>25+BB 适中→TRENDING，ADX<20+BB 窄→RANGING，BB 极宽→HIGH_VOLATILITY

**待开发**：

| 文件 | 内容 | 优先级 |
|-----|------|-------|
| `hmm_model.py` | HMM 3 状态模型，需离线训练后存 models/ | P2 |
| `strategy_switcher.py` | 制度变化时动态修改 config/risk.yml 参数 | P2 |

---

## 关键模式和约定

### 读取指标参数的标准方式

```python
# 所有指标参数必须这样读取，禁止在代码中写数字
import yaml
from pathlib import Path

def _load_config() -> dict:
    cfg_path = Path(__file__).parent.parent / "config" / "indicators.yml"
    with open(cfg_path) as f:
        return yaml.safe_load(f)

_cfg = _load_config()
RSI_PERIOD = _cfg["momentum"]["rsi"]["period"]      # 例：14
BBANDS_PERIOD = _cfg["volatility"]["bbands"]["period"]  # 例：20
```

### 指标函数标准签名

```python
import pandas as pd


def compute_momentum(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    输入：OHLCV DataFrame（列：open/high/low/close/volume）
    输出：追加了动量指标列的 DataFrame
    列命名约定：RSI_14，ROC_10，CCI_20，STOCH_K_14，STOCH_D_14
    所有指标用纯 pandas/numpy 实现，无第三方指标库依赖（如 pandas_ta/talib）。
    """
    period = cfg["rsi"]["period"]  # 从 config 读取
    # 纯 pandas RSI 实现（5 行）
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0).ewm(span=period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(span=period, adjust=False).mean()
    df["RSI_14"] = 100.0 - (100.0 / (1.0 + gain / loss.replace(0, float('nan'))))
    ...
    return df
```

### 写入 indicators Stream 的消息格式

```python
# 完整格式见 contracts/STREAM_SCHEMA.md
msg = {
    "symbol": "BTCUSDT",
    "timeframe": "1h",
    "ts": 1700000000000,
    "indicators": {
        "RSI_14": 58.3,
        "MACD_12_26_9": 120.5,
        "ATR_14": 380.2,
        # ... 所有计算出的指标
    }
}
```

### 缓存管理约定（cache_manager.py）

```python
# 慢周期 key 格式：indicators:{symbol}:{timeframe}:{ts}
# TTL：1d=86400s，4h=14400s，1h=3600s
# 快周期（5m/1m）不缓存，直接计算
SLOW_TIMEFRAMES = {"1d": 86400, "4h": 14400, "1h": 3600}
FAST_TIMEFRAMES = {"15m", "5m", "1m"}  # 不缓存
```

### HMM 模型约定（hmm_model.py）

```python
# 3 个状态：0=RANGING, 1=TRENDING, 2=HIGH_VOLATILITY
# 输入特征：ATR/close（波动率归一化）+ ADX + BB宽度
# 模型文件：regime/models/hmm_{symbol}_{n_states}.pkl
# 训练数据：只能用 validation/datasets/train/（铁律 #2）
```

---

## 测试要求

每个指标文件完成后必须对应 `tests/test_indicators_{name}.py`：
- 测试 NaN 处理（输入数据不足时）
- 测试边界值（价格为 0，成交量为 0）
- 测试配置读取（确认参数从 yml 读取，不是硬编码）
