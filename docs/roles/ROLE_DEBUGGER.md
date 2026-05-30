# ROLE_DEBUGGER.md — 生产诊断 + 故障排查

## ── 启动 Prompt（复制此块开启新会话）──

```
你是 crypto-ai-trader 的 SRE 工程师，专注于生产故障诊断和根因分析。

【必读文件】
1. ARCH.md — 架构速查卡（重点：Stream 流向图和熔断器恢复步骤）

【你不需要读的文件】
STATUS.md · 任何 ROLE_*.md（你只需要 ARCH.md + 下方提供的日志/告警信息）

【诊断时请提供以下信息】
- 告警类型（熔断/LLM失败率/因子衰减/Stream堆积/其他）
- 最近的结构化日志片段
- 最近的代码变更（git log --oneline -10）
- docker ps 输出（确认服务是否在跑）

【输出格式】
1. 快速定位（哪个 Stream/模块/服务出了问题）
2. 根因分析（为什么会这样）
3. 修复步骤（可直接执行的命令/代码）
4. 防止复发（建议的预防措施）
```

---

## 常见故障类型和诊断路径

### 1. 熔断器触发（circuit_breaker OPEN）

```bash
# Step 1: 查 decision_log 找触发原因
SELECT * FROM decision_log 
WHERE breaker_state = 'OPEN' 
ORDER BY ts DESC LIMIT 20;

# Step 2: 确认触发条件
# 回撤触发：看 drawdown_limit.py 的回撤计算
# 连续亏损触发：数 validated=True AND signal_sent=True 的连续亏损单

# Step 3: 风险确认后恢复
from risk_guardian.circuit_breaker import CircuitBreaker
CircuitBreaker().reset()  # 只在风险确实解除后执行

# 冷静期：默认 4 小时（config/risk.yml: CIRCUIT_COOLDOWN_HOURS）
```

### 2. LLM 失败率高（ai_signal Stream 停止流动）

```bash
# Step 1: 确认 LLM 服务状态
# 检查 decision_log 中 validated=False 的比率

SELECT 
    COUNT(*) FILTER (WHERE validated = false) * 100.0 / COUNT(*) as fail_rate,
    date_trunc('hour', ts) as hour
FROM decision_log 
WHERE ts > NOW() - INTERVAL '6 hours'
GROUP BY hour ORDER BY hour DESC;

# Step 2: 检查是 LLM 超时还是 Schema 校验失败
# 超时：llm_client.py 30s 超时日志
# Schema 失败：validated=False + reasoning 字段内容异常

# Step 3: fallback_handler 是否在工作？
# 检查 ai_signal Stream 中是否有 FLAT 信号（表示 fallback 在运行）

# Step 4: 临时缓解
# 修改 config/llm_prompts/market_analysis.j2 简化 Prompt（可能是 Prompt 太长导致超时）
# 然后 prompt_versioner.register() 更新版本
```

### 3. Stream 堆积（某个消费者停止消费）

```bash
# 检查 Stream 积压
redis-cli XLEN raw_kline
redis-cli XLEN indicators
redis-cli XLEN regime_signal
redis-cli XLEN ai_signal
redis-cli XLEN trade_order

# 积压 > 5000 条：backpressure.py 应已触发暂停生产者
# 查看消费者组状态
redis-cli XINFO GROUPS raw_kline

# 常见原因：
# indicators/ 某计算函数死循环 → 查 indicator-worker 容器日志
# ai-engine 服务重启后消费者组 pending 消息未 ACK → XACK 手动确认
docker logs indicator-worker --tail 100
docker logs ai-engine --tail 100
```

### 4. WS 断连后 K 线缺口

```bash
# gap_filler.py 应自动处理，如果没有：
# Step 1: 确认 last_ts 指针值
redis-cli GET "ws:last_ts:BTCUSDT:1h"

# Step 2: 手动触发补全（如果自动补全失败）
from data.gap_filler import GapFiller
await GapFiller().fill("BTCUSDT", last_ts=<上面查到的值>)

# Step 3: 如果缺口太大（>24小时），用 backfill 脚本
python scripts/backfill_data.py --symbol BTCUSDT --from <timestamp>
```

### 5. 因子 IC 衰减告警

```bash
# 查 InfluxDB 中的 IC 时序
# Grafana 仪表板：Factor IC Decay 面板
# IC < 0.02 持续 > 3 天 → 该因子可能已失效

# 处理步骤：
# 1. 确认是哪个因子（factor_decay_monitor.py 日志）
# 2. 在 prompt_builder.py 中临时降低该因子权重（不删除）
# 3. 触发 factor_mining.py 重新评估（注意：只在 train/ 数据上）
# 4. 如果 IC 持续为负，考虑从 Prompt 中移除该因子
```

---

## 快速诊断命令速查

```bash
# 服务状态
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# 各服务最近 50 行日志
docker logs data-collector --tail 50
docker logs indicator-worker --tail 50
docker logs ai-engine --tail 50
docker logs risk-guardian --tail 50

# Redis Stream 状态
redis-cli XLEN raw_kline && redis-cli XLEN indicators && redis-cli XLEN ai_signal

# TimescaleDB 最近决策
psql -U trader -c "SELECT ts,symbol,validated,breaker_state,signal_sent FROM decision_log ORDER BY ts DESC LIMIT 10;"

# 系统健康检查
python scripts/health_check.py
```

---

## 根因分析（RCA）模板

完成诊断后输出：

```markdown
## RCA — [故障描述] — [日期]

### 影响范围
- 持续时间：
- 影响的 Stream：
- 是否有真实交易损失：

### 根因
[一句话描述]

### 时间线
- HH:MM 发生了什么
- HH:MM 告警触发
- HH:MM 开始处理
- HH:MM 恢复

### 修复步骤（已执行）
1. ...

### 预防措施（建议添加）
1. ...

### 需要更新的文件
- [ ] ROLE_DEBUGGER.md（新增此类故障的诊断路径）
- [ ] alert_manager.py（是否需要更早告警）
```
