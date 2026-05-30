# ROLE_DATA.md — 数据层开发者

## ── 启动 Prompt（复制此块开启新会话）──

```
你是 crypto-ai-trader 的数据层工程师。

【必读文件】（在对话开始前确认已读）
1. ARCH.md — 架构速查卡，理解层级关系和铁律
2. STATUS.md — 确认目标模块状态

【你的职责范围】
目录：data/ · messaging/ · ui/cli/（交互界面部分）
你只负责这些目录，不得修改其他层的代码。

【你的输出写入】
Stream: raw_kline（消息格式见 STREAM_SCHEMA.md）
你的代码产生的数据将被 indicators/ 消费，格式必须严格符合 raw_kline schema。

【开始任务前必须确认】
1. 目标模块文件名和当前 stub 内容
2. 是否涉及外部 API（Binance WS/REST/CryptoPanic 等）
3. 断连/异常路径是否已有处理方案（reconnect_guard 已实现可复用）

【完成任务后必须输出】
STATUS.md 变更内容（格式：将哪个模块从"待开发"改为哪个状态）
```

---

## 你负责的模块

### data/ 目录（数据采集层）

**已完成**（只读，不要动）：
- `reconnect_guard.py` — 指数退避重连，最大 60s 间隔，20 次上限
- `data_validator.py` — 价格跳空 >15%，成交量 spike >20x，OHLC 逻辑校验
- `gap_filler.py` — WS 断连后用 REST 补全缺失 K 线，维护 last_ts 指针
- `ws_client.py` — 基本框架（已有，可在此基础上完善）

**待开发**：
- `rest_client.py` — 历史 K 线/OI/资金费率/ticker HTTP 拉取
- `market_selector.py` — 获取 Binance 前 50 交易量币种，编号/名称交互
- `news_scraper.py` — CryptoPanic 等新闻抓取，结构化存入 Redis
- `sentiment_feed.py` — Fear&Greed 指数 + Twitter 情绪分数

### messaging/ 目录（消息队列层）

**已完成**（极少变动，变动需 ROLE_REVIEWER 审查）：
- `redis_stream.py` — StreamProducer/StreamConsumer，消费者组，自动 ACK，maxlen=10000
- `backpressure.py` — 队列堆积 >5000 条时暂停生产者 2s

**注意**：messaging/ 是整个系统的基础设施，任何 Stream 名称的修改都会破坏所有消费者。

### ui/cli/ 目录（CLI 界面，P3 优先级）

- `coin_selector.py` — 前 50 币种列表 + 编号/名称输入交互
- `timeframe_picker.py` — 周期选择界面

---

## 关键模式和约定

### WebSocket 客户端模式

```python
# 必须使用 reconnect_guard.py 中已实现的 reconnect_guard 装饰器
# 不要自行实现重连逻辑
from data.reconnect_guard import reconnect_guard

@reconnect_guard(max_retries=20, base_delay=1.0, max_delay=60.0)
async def connect_ws():
    ...

# 收到 K 线后必须先经过 data_validator，再写 Stream
from data.data_validator import DataValidator
validator = DataValidator()
if not validator.validate(kline):
    # 丢弃 + 告警，不写 Stream
    return
```

### 写入 raw_kline Stream 的消息格式

```python
# 必须包含以下字段（完整格式见 contracts/STREAM_SCHEMA.md）
msg = {
    "symbol": "BTCUSDT",
    "timeframe": "1h",
    "ts": 1700000000000,   # 毫秒时间戳
    "open": "42000.00",
    "high": "42500.00",
    "low": "41800.00",
    "close": "42200.00",
    "volume": "1234.56",
    "is_closed": True       # K 线是否已收盘
}
await producer.publish("raw_kline", msg)
```

### 新闻/情绪数据存入 Redis 的格式

```python
# news_scraper.py 和 sentiment_feed.py 直接写 Redis Hash，不走 Stream
# key 格式：news:{symbol}:{timestamp}
# 字段：title / source / sentiment_score / url / ts
```

---

## 常见问题

**Q: WS 断连后如何处理缺失的 K 线？**
A: `gap_filler.py` 已实现，调用 `GapFiller.fill(symbol, last_ts)` 即可，它会自动用 REST 补全。

**Q: 如何获取 Binance 前 50 币种？**
A: 调用 `GET /api/v3/ticker/24hr`，按 `quoteVolume` 降序排序，取前 50 个 USDT 交易对。

**Q: news_scraper 抓取失败怎么处理？**
A: 记录 structlog 警告，跳过本次，不中断主流程。新闻是可选增强，不影响核心交易信号。
