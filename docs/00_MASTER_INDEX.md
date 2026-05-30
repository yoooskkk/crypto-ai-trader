# crypto-ai-trader — AI 协作开发体系总索引

> **这是整个文档体系的入口。** 人类开发者在此分配任务；AI 根据角色只读对应文件。
> 任何 AI 不得在未读完本角色规定文件的情况下开始编写代码。

---

## 一、文档体系全景

```
docs/
├── 00_MASTER_INDEX.md          ← 你现在读的这个（人类 + 所有 AI 均可读）
│
├── context/                    ← 系统级上下文（精简版，约 800 token）
│   ├── ARCH.md                 ← 架构速查：层级 · Stream名 · 铁律 · 禁止清单
│   └── STATUS.md               ← 开发状态（唯一需要频繁更新的文件）
│
├── roles/                      ← 每个 AI 角色的 Prompt 文件
│   ├── ROLE_DATA.md            ← 数据层开发者
│   ├── ROLE_INDICATORS.md      ← 指标层开发者
│   ├── ROLE_ANALYSIS.md        ← 分析 + AI引擎层开发者
│   ├── ROLE_RISK.md            ← 风控层开发者（最高安全等级）
│   ├── ROLE_INFRA.md           ← 基础设施 + 部署 + 可观测性
│   ├── ROLE_REVIEWER.md        ← 代码审查员（所有层通用）
│   └── ROLE_DEBUGGER.md        ← 生产诊断 + 故障排查
│
└── contracts/                  ← 层间契约（Stream 格式 + SLA）
    └── STREAM_SCHEMA.md        ← 每条 Stream 消息的精确 JSON Schema
```

---

## 二、角色 → 读哪些文件（上下文预算表）

| 角色 | 必读文件 | 按需读文件 | 跳过文件 | 估算 token |
|------|---------|-----------|---------|-----------|
| **数据层开发者** | ARCH.md · STATUS.md · ROLE_DATA.md | STREAM_SCHEMA.md | 所有其他 ROLE_*.md | ~1,800 |
| **指标层开发者** | ARCH.md · STATUS.md · ROLE_INDICATORS.md | STREAM_SCHEMA.md | 其他 ROLE_*.md | ~1,900 |
| **分析/AI引擎开发者** | ARCH.md · STATUS.md · ROLE_ANALYSIS.md | STREAM_SCHEMA.md · config/llm_prompts/ | 其他 ROLE_*.md | ~2,200 |
| **风控层开发者** | ARCH.md · STATUS.md · ROLE_RISK.md | STREAM_SCHEMA.md · config/risk.yml | 其他 ROLE_*.md | ~2,000 |
| **基础设施工程师** | ARCH.md · STATUS.md · ROLE_INFRA.md | docker-compose.yml · infra/ | 所有业务 ROLE_*.md | ~1,600 |
| **代码审查员** | ARCH.md · ROLE_REVIEWER.md | 被审查的具体文件 | STATUS.md · ROLE_*.md | ~1,500 |
| **生产诊断工程师** | ARCH.md · ROLE_DEBUGGER.md | observability/ · 日志片段 | 所有开发 ROLE_*.md | ~1,400 |

> **性价比原则**：每个角色的必读文件控制在 2000 token 以内，避免将整个 AI_CONTEXT.md 塞入每次对话。

---

## 三、任务分配流程（人类操作）

```
1. 确认要开发的模块（查 STATUS.md 第二节）
2. 对照下方映射表，选择正确角色
3. 打开对应 ROLE_*.md 文件，复制其 [Prompt 头部] 启动新会话
4. 将 ARCH.md + STATUS.md 内容粘贴进同一会话（或上传）
5. 粘贴目标模块的现有 stub 代码
6. AI 完成后，将 STATUS.md 更新内容粘贴回本地文件
```

---

## 四、模块 → 角色映射

| 模块目录 | 负责角色 |
|---------|---------|
| `data/` | ROLE_DATA |
| `messaging/` | ROLE_DATA（变更时需 ROLE_REVIEWER 审查）|
| `indicators/` | ROLE_INDICATORS |
| `regime/` | ROLE_INDICATORS（HMM部分）|
| `analysis/` | ROLE_ANALYSIS |
| `ai_engine/` | ROLE_ANALYSIS |
| `risk_guardian/` | ROLE_RISK（必须经 ROLE_REVIEWER 审查）|
| `validation/` | ROLE_ANALYSIS（验证逻辑）+ ROLE_RISK（数据隔离审查）|
| `freqtrade_strategies/` | ROLE_RISK |
| `observability/` + `security/` | ROLE_INFRA |
| `infra/` + `docker-compose.yml` | ROLE_INFRA |
| `config/` | 对应业务角色（改 risk.yml → ROLE_RISK）|
| `tests/` | 对应业务角色 + ROLE_REVIEWER 审查 |

---

## 五、跨角色协作规则

- **Stream 格式变更**：必须由 ROLE_REVIEWER 审查 STREAM_SCHEMA.md 后，同步通知上下游角色
- **risk_guardian 任何修改**：代码完成后强制经 ROLE_REVIEWER 走安全清单
- **STATUS.md 更新**：每个角色完成任务后，由该角色输出 STATUS.md 变更内容，人类手动合并
- **ARCH.md 变更**：需要人类决策，不由单一 AI 角色自行修改

---

*本文件版本：v1.0 | 与 AI_CONTEXT.md 同步*
