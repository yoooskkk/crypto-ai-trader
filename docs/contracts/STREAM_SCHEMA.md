# STREAM_SCHEMA.md — Redis Stream 消息契约

> 这是服务间通信的唯一契约文件。任何修改必须同步通知上下游所有角色。
> 修改此文件 = 破坏性变更，需要所有消费者同步更新。

---

## Stream 总览

| Stream 名 | 生产者 | 消费者 | 消息频率 |
|-----------|-------|-------|---------|
| `raw_kline` | data/ws_client.py | indicators/（所有） | 每根 K 线收盘 |
| `indicators` | indicator-worker | regime-worker · ai-engine | 每根 K 线收盘后 |
| `regime_signal` | regime/detector.py | ai-engine | 制度变化时（低频）|
| `ai_signal` | ai_engine/plan_generator.py | risk-guardian | 每次 LLM 完成 |
| `trade_order` | risk_guardian/ | freqtrade | 信号通过风控后 |

---

## raw_kline — K 线原始数据

```json
{
  "symbol":    "BTCUSDT",       // string，交易对
  "timeframe": "1h",            // string，枚举：1m/5m/15m/1h/4h/1d
  "ts":        1700000000000,   // int，毫秒时间戳（K 线开盘时间）
  "open":      "42000.00",      // string（保留精度）
  "high":      "42500.00",      // string
  "low":       "41800.00",      // string
  "close":     "42200.00",      // string
  "volume":    "1234.56",       // string（基础货币成交量）
  "is_closed": true             // bool，K 线是否已收盘
}
```

**约束**：
- `is_closed=false` 的 K 线可以发布，但下游必须识别并只在 `is_closed=true` 时计算指标
- 价格字段用 string 保留精度，下游转 Decimal 处理

---

## indicators — 计算后的指标快照

```json
{
  "symbol":    "BTCUSDT",
  "timeframe": "1h",
  "ts":        1700000000000,
  "indicators": {
    "EMA_9":       42150.5,
    "EMA_21":      41980.2,
    "EMA_55":      41200.0,
    "EMA_200":     39500.0,
    "SMA_20":      42000.0,
    "MACD_12_26_9": 120.5,
    "MACD_signal":  80.3,
    "MACD_hist":    40.2,
    "ADX_14":       28.5,
    "RSI_14":       58.3,
    "ATR_14":       380.2,
    "BBANDS_upper": 43200.0,
    "BBANDS_mid":   42000.0,
    "BBANDS_lower": 40800.0,
    "OBV":          123456789.0,
    "VWAP":         42100.0
  }
}
```

**约束**：
- 所有指标值为 float，NaN 用 null 表示（不得用 0 替代 NaN）
- 字段名必须与 indicators.yml 中的命名一致

---

## regime_signal — 市场制度信号

```json
{
  "symbol":     "BTCUSDT",
  "ts":         1700000000000,
  "regime":     "TRENDING",     // 枚举：TRENDING/RANGING/HIGH_VOLATILITY/UNKNOWN
  "confidence": 0.85,           // float，制度判断的置信度
  "method":     "rule_based",   // string：rule_based / hmm
  "prev_regime": "RANGING",     // string，前一个制度（用于检测切换）
  "adx":        28.5,           // 辅助字段，方便下游调试
  "bb_width":   0.042           // 辅助字段
}
```

---

## ai_signal — AI 生成的交易信号

```json
{
  "symbol":         "BTCUSDT",
  "ts":             1700000000000,
  "direction":      "LONG",         // 枚举：LONG/SHORT/FLAT
  "confidence":     0.82,           // float，0.0~1.0
  "entry":          42200.0,        // float，建议入场价
  "sl":             41500.0,        // float，止损价
  "tp":             43500.0,        // float，止盈价
  "score":          0.74,           // float，综合评分（signal_scorer 填充）
  "prompt_version": "a3f8c1d2",     // string，SHA1 前 8 位
  "regime":         "TRENDING",     // string，生成时的制度状态
  "reasoning":      "EMA多头排列，RSI未超买...",  // string，LLM 理由
  "is_fallback":    false           // bool，是否为 fallback 信号
}
```

---

## trade_order — 最终交易指令

```json
{
  "symbol":        "BTCUSDT",
  "ts":            1700000000000,
  "action":        "LONG",          // 枚举：LONG/SHORT/FLAT/FORCE_EXIT
  "size_pct":      0.08,            // float，建议仓位占总资产比例
  "entry":         42200.0,
  "sl":            41500.0,
  "tp":            43500.0,
  "source":        "ai_signal",     // string：ai_signal / freqtrade_native
  "breaker_state": "CLOSED",        // string，发出时的熔断器状态
  "audit_id":      "uuid-v4"        // string，用于 audit_logger 追踪
}
```

**约束**：
- `action=FORCE_EXIT` 只由 risk_guardian 的 circuit_breaker 触发
- `size_pct` 由 position_sizer.py 计算，上限受 risk.yml 约束
- Freqtrade 消费此 Stream 后，必须忽略 `size_pct` 以外的仓位建议（以 Freqtrade 自身的 stake_amount 为准）

---

## 版本管理

| 版本 | 变更 | 影响 |
|-----|------|------|
| v1.0 | 初始定义 | — |

**修改流程**：
1. 在此文件记录版本变更
2. 同步通知所有相关角色（ROLE_DATA / ROLE_INDICATORS / ROLE_ANALYSIS / ROLE_RISK）
3. 新旧格式并行期至少 1 个迭代，再删除旧字段
