# proto —— 最小原型（可运行）

> **所有细节以代码为准。** 设计依据、决策理由、实测数据见 `../docs/` ——
> 重点看 **`../docs/solution.md`**（方案文档）。

针对三个痛点（**P1** 重复问参数 / **P2** 没干完就重开 / **P3** 提问后走向不确定），
以及根本诉求（**上下文隔离，防止上下文爆炸**）的最小可跑原型。

## 图的形状

```
START → feed → prepare → agent → finalize → END
```

| 节点 | 干什么 | 硬/软 |
|---|---|---|
| `feed` | 用户这条消息标了 `answer_to` 就**定向投给那一个**待答任务（里面**同步跑执行器**） | **硬**（平台保证投递） |
| `prepare` | 把**任务台账**注入主 agent 上下文 | **硬**（平台保证注入） |
| `agent` | 主 agent —— 完整的 agent（自己的工具 + 自己的循环） | LLM 驱动 |
| `finalize` | 检查待答都问出去了吗，没问就补 | **硬**（保证不丢） |

## 三条要点

### 1. Skill / Task / Sub Agent —— 三者正交

| 概念 | 是什么 | 载体 | 生命周期 |
|---|---|---|---|
| **Skill** | 专业能力（静态） | `description` + `instructions` | 长期 |
| **Task** | 一次具体业务工作（动态） | `state["tasks"]` 里一条记录 | **跨轮存活**（同一 thread 内） |
| **Sub Agent** | **可选**执行者 | `create_agent` 建的一次性实例 | 用完即弃 |

**关键推论**：

- **Skill 不绑执行者** —— 加一个技能不需要动平台（`run_task(skill, given)` 通用）
- **执行器不持有状态** —— 状态在 Task 上，所以"重新起一个"是无害的
- **Task 只在主 agent 决定委派时才建** —— 不委派就没有 Task

### 2. 主 agent 是**完整的 agent**，不是纯路由器

自己决定走哪条：**直接答**（闲聊/追问，不建 Task）/ **`write_artifact` 自己产出短内容**（不建 Task）/ **`delegate` 委派**（建 Task）。

**技能活一律委派** —— 实测给了它"自己干技能活"的路，它就会走，**隔离全丢**。

### 3. 三态判定 + 出口限制（`delegate` 内部，平台代码，LLM 跳不过）

```
执行器干活（独立上下文，最多 3 轮）
   ↓
判定：completed / waiting_for_input / incomplete
   ├─ completed          → 交付
   ├─ waiting_for_input  → 平台强制先让主 agent 试答 → 答不上才问用户
   └─ incomplete         → 不许它停：带 progress/missing 推回去继续
                           推满 3 轮仍不行 → failed，交给主 agent
```

## 跑起来

```bash
# 1) 配模型（复制 .env.example 为 .env 并填好）
cp .env.example .env

# 2) 交互式
uv run python -m proto

# 3) 单轮（有待答会自动补一轮，模拟用户答复）
uv run python -m proto --say "帮我备课，三年级数学"
```

> ⚠️ **没有 fake / 离线模式** —— 所有 LLM 调用都走真实模型。
>
> 曾经有个 `PROTO_FAKE_MODEL=1`，2026-10-28 拆掉了，理由见 `config.py` 的模块 docstring。
> 一句话：它**只假一半**（主 agent 仍调真实 API），而且**短路了 `run_task` 的整个轮次循环** ——
> 拿它测多轮行为、出口限制、判定视野**全都测不到**，反而给出"跑通了"的假信号。

**建议这样试**：先问 `帮我备课和导学单，三年级数学`，看它**只问 2 个字段**
（`课题`、`课时`）而不是 6 个 —— `年级`、`学科` 被跨技能去重了。

每轮结束会打印 **主 agent 上下文体积 vs 产物库正文体积**，那是隔离效果的现场读数。

## 文件

| 文件 | 作用 |
|---|---|
| `config.py` | 模型配置（环境变量 / `.env`）—— **没有 fake / 离线模式** |
| `llm.py` | 结构化输出 + 兜底（DashScope 偶发 grammar 超时，一次抖动会炸掉整个 turn） |
| `skills.py` | Skill 定义（**只有 `description` + `instructions`**）+ 产物库 |
| `executor.py` | 执行器：**三态判定 + 出口限制** |
| `agent.py` | 主 agent：工具集 + 系统提示 + `delegate`（建 Task + 三态分派） |
| `graph.py` | 图：`feed` / `prepare` / `agent` / `finalize` + `_merge_tasks` reducer |
| `trace.py` | 全量日志到 `../logs/proto-<时间戳>.log`（含每次 LLM 调用的 PROMPT 与 RESPONSE 原文） |
| `__main__.py` | 交互式 CLI（显示 Task 列表、上下文体积、隔离比） |

## ⚠️ 原型与生产的差距（必读）

| 项 | 原型 | 生产 |
|---|---|---|
| **产物存储** | **内存 dict，重启即丢** | 你们的产物表 |
| **checkpointer** | `InMemorySaver`，重启即丢 | Sqlite / Postgres |
| **长期记忆** | 无 —— 跨会话参数复用做不到 | `store` / `memory_context` |

还有一条**模型前提**：执行器模式对模型能力有要求，**换模型必须重新验证**
（实测豆包在结构化输出上会**静默失效**）。见 `../docs/solution.md` §九。

## 验证脚本

在 `../experiments/`。文档引用、且**当前能跑**的两个：

```bash
uv run python -m experiments.debug_exit            # 出口限制（调真实模型）
uv run python -m experiments.probe_api             # API 签名核查（不调模型）
```

（`debug_judge_variants` 2026-11-06 已删 —— 判定退休后它验的东西不存在了。）
