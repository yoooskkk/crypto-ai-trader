# ROLE_INFRA.md — 基础设施 + 可观测性 + 安全层

## ── 启动 Prompt（复制此块开启新会话）──

```
你是 crypto-ai-trader 的基础设施工程师，负责系统的可靠运行、监控和安全。

【必读文件】
1. ARCH.md — 架构速查卡（重点：Docker Compose 服务依赖顺序 + 铁律 #4 密钥安全）
2. STATUS.md — 确认目标模块状态

【你的职责范围】
目录：observability/ · security/ · infra/ · scripts/
文件：docker-compose.yml · .env.example
不负责：任何业务逻辑代码

【核心约束】
- 铁律 #4：密钥永不出现在日志、代码、Git 提交中（你是密钥安全的最后防线）
- secrets_loader.py 已实现优先级：Docker Secrets > 环境变量 > .env
- 所有告警通过 alert_manager.py，不在业务代码中直接发送

【Docker 服务启动顺序（不得打乱）】
redis & timescaledb & influxdb → data-collector → indicator-worker →
regime-worker → ai-engine → risk-guardian → freqtrade

【完成任务后输出】STATUS.md 变更内容
```

---

## 你负责的模块

### observability/ 目录

**已完成（框架）**：
- `decision_logger.py` — 决策链路写入 TimescaleDB（框架完整，写入逻辑可完善）

**待开发/完善**：

| 文件 | 内容 | 优先级 |
|-----|------|-------|
| `alert_manager.py` | 熔断/因子衰减/LLM 失败率告警（钉钉/TG） | P1 |
| `factor_decay_monitor.py` | 定时 IC 计算，写 InfluxDB，Grafana 展示 | P2 |
| `grafana/dashboards/` | 仪表板 JSON 配置 | P2 |

### security/ 目录

**已完成（只读）**：
- `secrets_loader.py` — 密钥加载优先级链

**待开发**：

| 文件 | 内容 | 优先级 |
|-----|------|-------|
| `audit_logger.py` | 记录：谁/何时/触发了哪个信号/结果 | P1 |
| `api_key_rotator.py` | 定期轮换 Binance API Key，发送轮换提醒 | P2 |

### infra/ 目录

**已完成（只读）**：
- `timescaledb/init.sql` — 3 张超表：klines/indicators/decision_log

**可维护**：
- `redis/redis.conf` — maxmemory=512mb · allkeys-lru · appendonly yes
- `influxdb/` — 因子 IC 时序数据配置
- `prometheus/` — Prometheus 配置

---

## 关键约定

### 告警分级和路由（alert_manager.py）

```python
# 告警等级和触发条件
ALERTS = {
    "CRITICAL": [
        "circuit_breaker_triggered",   # 熔断触发
        "llm_failure_rate_high",       # LLM 失败率 > 30%（5分钟窗口）
    ],
    "WARNING": [
        "factor_decay_detected",       # 因子 IC < 0.02
        "exposure_limit_exceeded",     # 暴露度超限
        "ws_reconnect_count_high",     # 重连次数 > 10次/小时
    ],
    "INFO": [
        "new_regime_detected",         # 制度切换
        "prompt_version_changed",      # Prompt 版本更新
    ]
}
# 发送目标：CRITICAL → 钉钉+TG，WARNING → TG，INFO → 只记录日志
```

### decision_logger.py 写入字段（TimescaleDB decision_log 超表）

```python
# 这些字段必须完整，不得省略
log_entry = {
    "ts": datetime,           # 时间戳（超表分区键）
    "symbol": str,            # 交易对
    "timeframe": str,         # 时间周期
    "prompt_version": str,    # SHA1 版本号
    "regime": str,            # TRENDING/RANGING/...
    "validated": bool,        # LLM 输出是否通过 schema 校验
    "direction": str,         # LONG/SHORT/FLAT
    "confidence": float,      # 0.0~1.0
    "breaker_state": str,     # OPEN/CLOSED
    "signal_sent": bool,      # 信号是否最终发出
}
```

### 密钥安全检查清单（audit_logger.py 实现时必须遵守）

```python
# 禁止记录的字段（敏感信息绝不入日志）
FORBIDDEN_LOG_FIELDS = [
    "api_key", "secret_key", "password", "token",
    "binance_api", "binance_secret", "openai_key",
    "anthropic_key", "telegram_token"
]

def safe_log(event: dict) -> dict:
    """过滤敏感字段后再记录"""
    return {k: v for k, v in event.items() if k not in FORBIDDEN_LOG_FIELDS}
```

### Docker Compose 健康检查（docker-compose.yml 维护时）

```yaml
# 每个服务必须有 healthcheck，确保依赖链可靠
healthcheck:
  test: ["CMD", "python", "-c", "import redis; redis.Redis().ping()"]
  interval: 10s
  timeout: 5s
  retries: 3
  start_period: 30s
```

---

## 运维脚本（scripts/）

| 脚本 | 用途 | 触发时机 |
|-----|------|---------|
| `setup.sh` | 一键初始化（创建 secrets 目录/拉取镜像） | 首次部署 |
| `backfill_data.py` | 历史 K 线回填到 TimescaleDB | 新币种上线前 |
| `run_backtest.sh` | 触发 Freqtrade 回测 | 策略更新后 |
| `health_check.py` | 检查所有服务健康状态 | 定时巡检/告警后 |

---

## Grafana 仪表板应包含的面板

1. **交易决策链路**：decision_log 中 validated=False 的比率趋势
2. **熔断器状态**：breaker_state 时间轴，OPEN 段高亮红色
3. **LLM 延迟**：p50/p95/p99 响应时间
4. **因子 IC 衰减**：各因子 IC 值随时间变化（来自 InfluxDB）
5. **服务健康**：各 Docker 服务 up/down 状态
