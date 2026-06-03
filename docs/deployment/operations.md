# crypto-ai-trader 运维手册

> 日常运维操作、监控检查、故障恢复标准流程。

---

## 目录

1. [日常检查](#1-日常检查)
2. [健康检查解读](#2-健康检查解读)
3. [日志分析](#3-日志分析)
4. [备份策略](#4-备份策略)
5. [监控和告警](#5-监控和告警)
6. [模型管理](#6-模型管理)
7. [运维 SOP](#7-运维-sop)

---

## 1. 日常检查

### 每日检查清单

```shell
# 1. 容器状态
docker compose ps

# 2. Redis Stream 堆积情况
docker compose exec redis redis-cli INFO memory | grep used_memory_human
for stream in raw_kline indicators regime_signal ai_signal trade_order; do
  len=$(docker compose exec redis redis-cli XLEN $stream 2>/dev/null)
  echo "$stream: $len"
done

# 3. 最近 10 分钟错误日志
docker compose logs --since=10m | grep -iE "error|exception|critical" | tail -20

# 4. TimescaleDB 连接数
docker compose exec timescaledb psql -U trader crypto_trader -c \
  "SELECT count(*) FROM pg_stat_activity;"

# 5. 磁盘使用
df -h /var/lib/docker
```

### 健康指标解读

`scripts/health_check.py --json` 输出示例：

```json
{
  "redis": {"status": "ok", "latency_ms": 0.5},
  "timescaledb": {"status": "ok", "latency_ms": 2.1},
  "freqtrade_api": {"status": "ok", "latency_ms": 15.3},
  "influxdb": {"status": "ok", "latency_ms": 3.2},
  "summary": {
    "total": 4,
    "healthy": 4,
    "degraded": 0,
    "failed": 0
  }
}
```

| 状态 | 含义 | 处理 |
|:-----|:-----|:-----|
| `ok` | 正常 | 无需操作 |
| `degraded` | 延迟偏高 | 监控，检查网络/磁盘 |
| `failed` | 不可达 | 立即排查（见 troubleshooting） |

---

## 2. 日志解读

### 关键字速查表

| 日志信息 | 级别 | 含义 |
|:---------|:----:|:-----|
| `缓存预热中` | DEBUG | 指标缓存不足 200 根 K 线，正常 |
| `指标计算完成` | INFO | 正常 |
| `指标数据为空，跳过制度识别` | WARNING | 可能上游异常或数据延迟 |
| `制度识别完成` | INFO | 正常，包含当前制度和置信度 |
| `制度切换` | INFO | 市场制度发生变化 |
| `AI 信号生成完成` | INFO | 正常 |
| `AI 信号生成完成（低分信号）` | INFO | AI 置信度低于阈值，信号将被风控过滤 |
| `风控审核通过` | INFO | 信号通过检查，发出交易指令 |
| `风控过滤信号` | INFO | 信号被拒绝，含原因 |
| `熔断器已打开` | CRITICAL | 连续亏损触发熔断，停止所有开仓 |
| `回撤触发强平等级` | CRITICAL | 月/周回撤超限，即将强平所有持仓 |
| `强平成功/失败` | CRITICAL/ERROR | 强平操作结果 |

### JSON 日志查询

生产环境推荐 `LOG_JSON=true`，配合 `jq` 过滤：

```shell
# 查看所有信号
docker compose logs --since=1h ai-engine | \
  jq 'select(.event == "AI 信号生成完成") | {symbol, direction, confidence, score}'

# 查看错误
docker compose logs --since=24h | \
  jq 'select(.level == "error" or .level == "critical") | {time: .timestamp, event, error}'

# 查看熔断状态变化
docker compose logs --since=24h risk-guardian | \
  jq 'select(.event | startswith("熔断")) | {event, state, consecutive_losses}'
```

### 日志文件位置

Docker 容器内日志写入 stdout/stderr，由 Docker 管理：
```shell
docker compose logs -f --tail=100 data-collector
```

持久化日志可通过 Docker 日志驱动配置：
```yaml
# docker-compose.yml 中添加
x-logging: &default-logging
  driver: "json-file"
  options:
    max-size: "10m"
    max-file: "3"

services:
  indicator-worker:
    logging: *default-logging
```

---

## 3. 数据备份

### 3.1 TimescaleDB 备份

```shell
# 全量备份
docker compose exec -T timescaledb pg_dump \
  -U trader -Fc crypto_trader > backup_$(date +%Y%m%d_%H%M%S).dump

# 恢复
docker compose exec -T timescaledb pg_restore \
  -U trader -d crypto_trader --clean backup.dump

# 自动备份脚本（crontab 每日）
0 2 * * * cd /opt/crypto-ai-trader && \
  docker compose exec -T timescaledb pg_dump -U trader -Fc crypto_trader \
  > backups/db_$(date +\%Y\%m\%d).dump && \
  find backups/ -name "db_*.dump" -mtime +30 -delete
```

### 3.2 Redis 备份

```shell
# 触发 Redis 持久化（RDB 快照）
docker compose exec redis redis-cli BGSAVE

# 手动备份 RDB 文件
docker compose cp redis:/data/dump.rdb ./backups/redis_dump.rdb

# 恢复
docker compose cp ./backups/redis_dump.rdb redis:/data/dump.rdb
docker compose restart redis
```

### 3.3 InfluxDB 备份

```shell
# 导出 bucket
docker compose exec influxdb influx backup \
  --token $INFLUXDB_TOKEN \
  /tmp/influx_backup

# 复制到宿主机
docker compose cp influxdb:/tmp/influx_backup ./backups/influx_$(date +%Y%m%d)
```

---

## 4. 监控和告警

### 4.1 Prometheus 指标

系统通过 Prometheus 暴露以下指标（路径 `/metrics`，端口见 `infra/prometheus/prometheus.yml`）：

| 指标 | 类型 | 标签 | 说明 |
|:-----|:-----|:-----|:-----|
| `signals_total` | Counter | symbol, direction, approved | 信号总数 |
| `trades_opened_total` | Counter | symbol, regime | 开仓数 |
| `drawdown_current_pct` | Gauge | — | 当前回撤百分比 |
| `circuit_breaker_state` | Gauge | — | 熔断器状态（0=关闭, 1=打开） |
| `factor_ic_value` | Gauge | factor_name | 因子 IC 值 |
| `redis_stream_length` | Gauge | stream | Redis Stream 堆积长度 |

### 4.2 Grafana 面板

预配置面板位于 `observability/grafana/dashboards/trading_system.json`，分为：

- **系统状态**：容器健康、Redis Stream 长度、CPU/内存
- **因子衰减**：各因子 IC 时间序列、半衰期
- **告警历史**：AlertManager 触发的所有告警

### 4.3 告警规则

预配置告警规则位于 `infra/prometheus/rules.yml`：

```yaml
groups:
  - name: trading_system
    rules:
      - alert: RedisStreamBacklog
        expr: redis_stream_length > 5000
        for: 5m
        annotations:
          summary: "Redis Stream 堆积超过 5000 条"

      - alert: CircuitBreakerOpen
        expr: circuit_breaker_state == 1
        annotations:
          summary: "熔断器已打开"
```

### 4.4 告警通道

支持 Telegram 和 Slack：

```shell
# 配置 Telegram（需在 .env 中设置）
ALERT_TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234
ALERT_TELEGRAM_CHAT_ID=-1001234567890

# 配置 Slack
ALERT_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T00/B00/xxx
```

---

## 5. 模型管理

### 5.1 HMM 模型

```shell
# 训练单个模型
docker compose exec regime-worker python -m scripts.train_hmm \
  --symbol BTCUSDT --timeframe 1h

# 训练所有主要币种
docker compose exec regime-worker python -m scripts.train_hmm \
  --all-major --timeframe 1h --concurrency 3

# 查看已训练模型
docker compose exec regime-worker python -m scripts.train_hmm --list-models

# 强制重新训练
docker compose exec regime-worker python -m scripts.train_hmm \
  --symbol BTCUSDT --timeframe 1h --force-refresh
```

### 5.2 数据回填

如果需要补充历史 K 线数据：

```shell
# 回填 BTCUSDT 1h K 线（最近 200 根）
docker compose exec data-collector python -m scripts.backfill_data \
  --symbol BTCUSDT --timeframe 1h --limit 200

# 回填所有主要币种
docker compose exec data-collector python -m scripts.backfill_data \
  --all-major --timeframe 1h --days 30 --concurrency 3

# 查看支持的交易所和交易对
docker compose exec data-collector python -m scripts.backfill_data \
  --list-symbols
```

---

## 6. 标准运维流程 (SOP)

### SOP-001: 启动系统

```shell
# 标准启动
docker compose up -d
sleep 30
docker compose ps
docker compose exec health-check python -m scripts.health_check --json
```

### SOP-002: 停止系统

```shell
# 优雅停止（会等待正在处理的信号完成）
docker compose stop

# 完全停止并清理
docker compose down

# 停止并清理数据卷（危险 — 会丢失所有数据）
# docker compose down -v
```

### SOP-003: 重启单个服务

```shell
# 安全重启（不会影响其他服务）
docker compose restart indicator-worker

# 重建并重启（代码变更后）
docker compose build indicator-worker
docker compose up -d indicator-worker
```

### SOP-004: 排查数据流中断

```shell
# 1. 检查数据采集
docker compose logs --tail=50 data-collector
docker compose exec redis redis-cli XLEN raw_kline

# 2. 如果 raw_kline 为空：
#    a) 网络问题 → 检查 Binance 可达性
#    b) API Key 问题 → 检查 secrets/binance_api_key.txt
#    c) WebSocket 连接 → 检查 data-collector 日志

# 3. 如果 raw_kline 有数据但 indicators 为空：
docker compose logs --tail=50 indicator-worker
#    a) 缓存预热中（< 200 根 K 线）→ 等待
#    b) 指标计算异常 → 检查日志中的 error 信息

# 4. 检查 Stream 流动
for stream in raw_kline indicators regime_signal ai_signal trade_order; do
  echo "$stream: $(docker compose exec redis redis-cli XLEN $stream)"
done
```

### SOP-005: 熔断器恢复

当熔断器触发后，自动进入冷却期（默认 2 小时）。如需手动恢复：

```shell
# 方法 1：重启 risk-guardian（重置熔断器状态）
docker compose restart risk-guardian

# 方法 2：通过 Dashboard API（未来功能）
# curl -X POST http://localhost:8080/api/risk/reset-breaker
```

**注意**：只有在确认导致熔断的根因已修复后才能手动恢复。

### SOP-006: 全系统升级

```shell
# 1. 备份
docker compose exec -T timescaledb pg_dump -U trader -Fc crypto_trader \
  > pre_upgrade_$(date +%Y%m%d).dump

# 2. 拉取新代码
git pull origin main

# 3. 检查变更
git log --oneline HEAD..HEAD~1

# 4. 构建部署
docker compose build --no-cache
docker compose up -d

# 5. 验证
sleep 60
docker compose ps
docker compose exec health-check python -m scripts.health_check --json
```

### SOP-007: 紧急强平

如果需要手动强平所有持仓（无论是否触发熔断）：

```shell
# 通过 risk-guardian 调用 Freqtrade API
docker compose exec risk-guardian python -c "
from risk_guardian.freqtrade_client import FreqtradeClient
client = FreqtradeClient()
result = client.force_exit_all()
print('Trade ID:', result.trade_id, '| Success:', result.success)
"
```

---

## 7. 性能调优

### 7.1 指标缓存大小

`indicators/processor.py` 中的 `_CACHE_SIZE = 300` 控制每对 (symbol, timeframe) 的最大缓存数。

- 增大（如 500）：需要更多内存，但减少数据丢失风险
- 减小（如 200）：节省内存，但需要更频繁的预热

### 7.2 Redis Stream 长度

Stream 默认最大长度 10,000 条（`messaging/redis_stream.py` 中 `maxlen=10_000`）。

当 Stream 堆积超过 5,000 条时，背压机制会触发消费者减速。如果频繁触发：

1. 增加 worker 数量（增加消费者组中的消费者）
2. 减少 `MAX_PENDING`（`messaging/backpressure.py` 中的 `MAX_PENDING = 5_000`）
3. 检查下游处理速度

### 7.3 LLM 请求限流

AI 引擎每次制度信号到达时调用一次 LLM。如果 Freqtrade 策略触发高频信号：

在 `ai_engine/processor.py` 中添加限流（当前未实现）：

```python
import asyncio
_llm_cooldown = 60  # 秒

async def process_regime_signal(message):
    elapsed = time.time() - _last_llm_call
    if elapsed < _llm_cooldown:
        return None  # 跳过此信号
    _last_llm_call = time.time()
    # ... 正常处理
```
