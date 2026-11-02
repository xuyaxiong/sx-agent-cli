# 当前架构（**现状说明书**）

> 🔴 **这份文档是画图的唯一依据** —— `figures/*.svg` 按它画。**改图先改这里**，
> 图与本文不一致时，以本文为准。
>
> 对应代码：`../proto/`（9 个模块）+ `../evals/`（装配与评测）。
> 设计原则与需求锚点见 `solution.md` §0；完成判定见 `solution.md` §十一；
> 评审结论见 `architecture-review.md`；场景回归见 `scenarios.md`。
>
> **更新于 2026-11-02**（对齐到 commit `bea6c4c`）

---

## 〇、怎么读这份文档

四条线，**每条都给字段级的事实**（不给"大概是"）：

| # | 这条线回答 | 在哪一节 |
|---|---|---|
| 一 | **模块划分** —— 谁负责什么、边界在哪、依赖谁 | §一 |
| 二 | **数据如何流动** —— 一次 `graph.invoke` 里数据经过谁的手 | §二 |
| 三 | **数据交接** —— **每一跳的精确内容**（交接了什么、谁生产、谁消费） | §三 |
| 四 | **软判断 / 硬判断** —— 哪些决定是 LLM 下的、哪些是代码保证的 | §四 |

配套两节字典：**§五 字段字典**（Task / ExecOutcome / _Judgement / Artifact / State）、
**§六 图与生命周期**。

> 📌 **为什么"交接"要单独成节**：这套系统的行为几乎全由"喂给 LLM 的上下文"决定 ——
> 交接多了（噪音）或少了（信息不足）、措辞差一点，**同一个模型会走出完全不同的路**。
> 用户 2026-10-30：*"一定要清晰准确打印**涉及上下文交接的所有地方**……
> 往往交接的东西**多一些或者少一些、引导有些区别，决策结果大大不同**。"*

---

## 一、模块划分

### 1.1 模块地图（谁负责什么 · **不**负责什么）

**`proto/` —— 运行期（产品本体）**

| 文件 | 领域 | 负责 | **不**负责 | 行数 |
|---|---|---|---|---|
| `config.py` | 配置 | `app_root()`（冻结后 = exe 目录）· `RESOURCE_ROOT` · `.env` 加载 · **三流 UTF-8** · `get_model()` · `describe()` | 不碰任何业务 | 96 |
| `llm.py` | LLM 公共 | `safe_structured()`（重试 1 次 → 保守默认）· 压掉 pydantic 序列化警告 | 不知道谁在调它 | 72 |
| `skills.py` | **技能领域** | `Skill`（`name`/`description`/`instructions`）· `SKILLS` 表 · `read_skill` 工具 · `catalog()` | **不做磁盘 I/O** | 140 |
| `skill_pack.py` | **技能包 I/O** | 文件夹形态 → `Skill`：`split_frontmatter` / `load_skill_pack` / `load_skills_dir`；自带文件的**列与读**（`list_skill_files` / `read_skill_file`） | 不定义 Skill 的语义 | 212 |
| `artifacts.py` | **产物领域** | 产物库 `ARTIFACTS` · ID 发号器 · 类型→扩展名**硬性派发** · 当场落盘 · `write/read/list_artifact` | 不管技能 | 175 |
| `executor.py` | 执行器 | `_PLATFORM_RULE` · 三态判定 · 出口限制循环 · `run_task()` · `ExecOutcome` · **`read_user_turn`**（闭包造，见 §3.2.2） | **不持有状态、不跨轮、不直接问用户** | 534 |
| `agent.py` | 主 agent | `_MAIN_PROMPT` · `delegate` 工具（含平台强制的四步）· **交接书** `render_briefing` · **用户原话快照** `_user_turns` · `absorb_outcome` | **不解析、不抽取参数** | 521 |
| `graph.py` | 图 | `State`/`AgentState` · `prepare`（投递 + 台账注入）· `agent` · `finalize`（透传）· `build()` | 不做业务判断 | 332 |
| `trace.py` | 可观测 | 全量日志 · **逐 super-step + 逐字流式**（`stream_agent`）· 参数摘要策略（`brief_args`） | 不截断正文（记录层不砍） | 217 |
| `__main__.py` | 裸原型 CLI | 交互式跑内置玩具技能 | — | 141 |

**`evals/` —— 装配 + 评测（原型的外部脚手架，不是产品本体）**

| 文件 | 负责 | 行数 |
|---|---|---|
| `harness.py` | **装配 + 记录层**：`install()`（装技能）· `snapshot()` · 路径约定（`FROZEN`/`ROOT`/`RUNS_DIR`）· `ensure_app_dirs()` · `dump_artifacts()` / `dump_run_record()` · `ctx_warn()` · **回执解析**（`_RECEIPT_KINDS` / `_split_receipt`） | 746 |
| `chat.py` | **交互式入口**（你当用户）：CLI 版式 · 主循环 · 实时 tail 日志 | 713 |
| `run.py` | **跑批**：case → 子进程 → 汇总 | 256 |
| `_child.py` | 跑批的**子进程**：跑一个 case 的一遍 | 119 |
| `checks.py` | **硬判断**（代码断言） | 520 |
| `judge.py` | **软判断**（LLM 评分）+ **留档** | 200 |
| `report.py` | 汇总出 `report.md` | 178 |
| `unit_*.py` | 纯函数单测（不调 LLM） | 491 |

### 1.2 依赖方向（**单向，无环**）

```
config ──► trace ──► (所有模块都在写日志)
  │
  ├─► llm ──► executor ──► agent ──► graph
  │              ▲            ▲          │
  ├─► skills ────┘            │          │
  ├─► skill_pack ─────────────┘          │
  └─► artifacts ─────────────────────────┘
```

**两条边界纪律**（都是踩出来的）：

| 纪律 | 为什么 |
|---|---|
| **`executor` 与 `agent` 互相不 import** —— 公共的 LLM 调用放 `llm.py` | 否则成环 |
| **`skills` / `artifacts` 是两个领域**（2026-10-30 拆开） | 变化原因不同：技能规范变了 vs 交付机制变了。原来挤在一个文件里叫"Skill 定义 + 产物库"，边界不清 |

> 🔴 **`read_skill` 在 `skills.py` 里延迟 import `proto.agent.SKILLS`** ——
> 技能表的**真值以 `proto.agent.SKILLS` 为准**（`delegate` 读的就是它，评测会替换它）。
> 延迟导入是为了避开 `agent ↔ skills` 的模块级成环。

### 1.3 三个领域对象 + 一条不变量

| 对象 | 在哪 | 是什么 |
|---|---|---|
| **Skill** | `skills.py` | **静态能力包**：`name` + `description` + `instructions`（**只有这三个字段**） |
| **Task** | `graph.State["tasks"]` | **一次具体业务工作**：跨轮存活，14 个字段见 §5.1 |
| **Artifact** | `artifacts.py` | **运行期交付物**：7 个字段见 §5.4 |

> 🔑 **一条不变量**：`read_skill_file` **只认 `list_skill_files` 给得出来的东西**。
> 路径穿越（`../`）、二进制、超大文件，全被这一条挡住 —— 不需要再写一套路径检查。

---

## 二、数据如何流动

### 2.1 一次 `graph.invoke` 的完整时序

```
  用户（CLI）
     │  HumanMessage(text)
     ▼
┌─────────────────────────────────────────────────────────────────────┐
│ ① prepare（平台节点 · 纯代码）                                        │
│                                                                     │
│   ⓪ _feed_waiting：把用户这条消息**原话**投给所有 waiting_for_input 的 Task │
│        └─► run_task(skill, render_briefing(rec))   ← 重入执行器        │
│        └─► absorb_outcome(rec, outcome)  → 结果写回 Task              │
│        ⚠️ 不判断"用户是不是在回答" —— 那是语义判断，平台不做            │
│                                                                     │
│   ① 渲染台账 → SystemMessage【平台提示】注入 messages                  │
│        ⚠️ 顺序不能反：先投递，台账才反映投递后的新状态                  │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ state["messages"] += SystemMessage(台账)
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ ② agent（主 agent 子图 · **完整的 agent**：自己的模型 + 工具 + 循环）   │
│      create_agent(model, tools=[6 个], system_prompt=_MAIN_PROMPT)    │
│      每走一步都 dump（trace.stream_agent：values + messages 两支）      │
│                                                                     │
│      三条出路，**它自己选**：                                          │
│        · 直接答            —— 闲聊/追问/澄清（**不建 Task**）          │
│        · write_artifact    —— 自己产出短内容（**不建 Task**）          │
│        · delegate(...)     —— 需要专业能力时（**建 Task**）            │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ delegate(skill_name, task, artifacts=[...])
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ ③ delegate 工具内部（**平台代码，这几步 LLM 跳不过**）                  │
│                                                                     │
│   ⓪ 校验：技能名不存在 → 拒绝；产物 ID 不存在 → 拒绝（**都不建 Task**）  │
│   ⓪' _supersede_old：同 skill 下 stage=failed 的旧 Task → superseded   │
│   ① render_briefing(rec) → run_task()  ← 执行器（隔离上下文）          │
│   ② 按**三态穷举**分派（没有第四种）：                                  │
│        completed ─────────────────────────► 交付（产物 ID + 路径 + 摘要）│
│        waiting_for_input ─► _ask_main（**平台强制试答**）              │
│                              ├─ 答得上 → 带补充重跑 → completed? ─► 交付│
│                              └─ 答不上 → 登记待答，交给平台转达         │
│        incomplete（推满 3 轮）─────────────► failed，交给主 agent       │
│                                                                     │
│   ③ 返回 Command(update={messages:[ToolMessage], tasks:[rec, *旧的]}) │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ ToolMessage（摘要 + 产物 ID + 文件路径）
                           ▼
              主 agent 看到工具返回 → 继续循环或给出最终回复
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ ④ finalize（平台节点 · 纯代码）                                       │
│     有待答 → **无条件**把执行器原话作为 AIMessage 贴进回复（透传）      │
│     没有   → 什么都不做                                              │
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
                        END → checkpointer 存 state（InMemorySaver）
```

### 2.2 执行器内部（`run_task`）：出口限制循环

```
run_task(skill, briefing)
  │  task_text = 【能力指令】instructions + 【技能自带文件】清单 + 【已知信息】briefing
  │  messages = [HumanMessage(task_text)]
  │
  ├─ 第 1 轮 ─────────────────────────────────────────────────────┐
  │    stream_agent(executor_agent)   ← 工具：write/read_artifact  │
  │    _recover_artifacts(resp, preexisting)  ← 只认**这次新写的**   │
  │    _last_ai_text(resp)                    ← report（只给摘要用） │
  │    _judge_material(arts, resp)            ← 判定看的"执行结果"   │
  │    _judge(skill, arts, material, briefing) → _Judgement         │
  │      ├─ completed / waiting_for_input ─────────────────► 出口    │
  │      └─ incomplete ─► 还有轮次？ ─► _nudge_text(j) 追加 HumanMessage
  │                        └─ 推满了 ─────────────────────► 出口（incomplete）
  ├─ 第 2 轮 …（同上，messages 累积）
  └─ 第 3 轮 …（同上，不再推）
  │
  ▼
ExecOutcome{status, artifacts, report, rounds, progress, missing}
```

> ⚠️ **`messages` 是累积的**（`messages = list(resp["messages"]) + [HumanMessage(nudge)]`）——
> 所以第 2 轮的 `resp` **包含第 1 轮的全部内容**。判定取材必须覆盖**所有轮次**（§3.2.3）。

### 2.3 数据流向总图（谁读谁写）

| 数据 | 生产者 | 存储 | 消费者 |
|---|---|---|---|
| 用户原话 | 用户 | `state["messages"]`（HumanMessage） | 主 agent · `_feed_waiting` |
| **台账** | `graph.node_prepare` | `state["messages"]`（SystemMessage） | 主 agent |
| **Task** | `agent.delegate` | `state["tasks"]`（按 id 合并） | 台账渲染 · `_feed_waiting` · `_supersede_old` |
| **交接书** | `agent.render_briefing` | 不存（现场渲染） | 执行器（作为 HumanMessage） |
| 产物正文 | 执行器 / 主 agent 的 `write_artifact` | `artifacts.ARTIFACTS`（+ 磁盘文件） | 判定 · `read_artifact` · 用户（打开文件） |
| 产物 ID | 同上（返回值） | Task 的 `artifacts` / `refs` | 主 agent · 台账 · 判定 |
| 判定结果 | `executor._judge` | `ExecOutcome` → Task 的 `progress`/`missing`/`stage` | 执行器（推回去）· 主 agent（台账 / 工具返回）· 用户（透传） |
| 轨迹日志 | `trace.*` | `logs/proto-<时间戳>.log` | 人（事后复盘）· CLI（tail） |

### 2.4 跨轮：Task 靠什么活着

**`Task` 不在执行器上，在图状态里** —— 靠 `InMemorySaver` checkpointer 跨轮存活
（同一个 `thread_id`）。执行器**用完即弃**，所以"重新起一个"是无害的：状态全在 Task 上。

> ⚠️ **前提**：同一会话的 `thread_id` 必须跨轮稳定。每轮换新 `thread_id` 会**静默**退化成
> "每次都是新会话"，Task / 台账 / supersede 全部失效，**而且不报错**。

---

## 三、数据交接（**每一跳的精确内容**）

### 3.1 交接总表

| # | 交接 | 从 → 到 | 载体 | 精确内容 |
|---|---|---|---|---|
| ① | 用户输入 | 用户 → 图 | `HumanMessage` | 原话（不加工） |
| ② | **台账注入** | 平台 → 主 agent | `SystemMessage` | 见 §3.2.1 |
| ③ | **派发** | 主 agent → 平台 | `delegate` 工具参数 | `skill_name` / `task` / `artifacts[]` |
| ④ | **交接书** | 平台 → 执行器 | `HumanMessage` | 见 §3.2.2 |
| ⑤ | 执行器产出 | 执行器 → 产物库 | `ARTIFACTS` + 磁盘 | 7 字段（§5.4） |
| ⑥ | **判定输入** | 平台 → 判定 | 结构化调用 prompt | 见 §3.2.3 |
| ⑦ | 判定结果 | 判定 → 平台 | `_Judgement` | `status` / `progress` / `missing` / `reason` |
| ⑧ | **推回去** | 平台 → 执行器 | `HumanMessage`（追加） | 见 §3.2.4 |
| ⑨ | **试答** | 平台 → 主 agent | 结构化调用 prompt | 见 §3.2.5 |
| ⑩ | **工具返回** | 平台 → 主 agent | `ToolMessage` | 见 §3.2.6 |
| ⑪ | **待答透传** | 平台 → 用户 | `AIMessage` | 执行器原话（`report`），**无条件** |
| ⑫ | **答复投递** | 平台 → 执行器 | `HumanMessage`（新 run_task） | 见 §3.2.7 |
| ⑬ | 产物路径 | 平台 → 主 agent | `ToolMessage` 附注 | `art_001 → <真实路径>` |

### 3.2 逐跳展开

#### 3.2.1 ② 台账注入（`graph.node_prepare`）

```
【平台提示】你已委派出去的工作（共 {N} 项）：
- {task_id}  「{skill}」  {状态标记}{→ 产物 ID}
      已做到：{progress 前 150 字}          ← 仅 stage ∈ (waiting_for_input, failed)
      还缺：{missing 逐条，前 200 字}        ← 同上
      执行器说：{report 前 150 字}           ← 同上

{提示句}
```

| 元素 | 取值 |
|---|---|
| 状态标记 `_STAGE_MARK` | `⏳ 进行中` / `✅ 已完成` / `⚠️ 等用户答复` / `❌ 失败` / `🔁 已被新的委派取代（不用管了）` |
| 展开细节的 `_NEEDS_DETAIL` | `("waiting_for_input", "failed")` —— 其余只列一行 |
| 提示句（**有待答时**） | 「**「等用户答复」的那些，平台已经负责把问题转达给用户、也会把用户的答复投回去** —— 你**不用**重新委派它们，更不要自己替用户编一个答案。」 |
| 提示句（**无待答时**） | 「（用户补充信息时，**只重跑受影响的任务**，已完成且不受影响的不动。）」 |
| 注入条件 | `tasks` 为空 → **不注入**（闲聊轮次里主 agent 上下文里没有这段） |

#### 3.2.2 ④ 交接书（`agent.render_briefing`）—— **所有重入路径的唯一入口**

```
【任务】（**主 agent 交代的** —— 它根据对话写的，可能补了用户没说的东西）
{task}

【用户原话】（**用户说的** —— 平台直接搬，未经转述）
- **第 N 轮（M 字）—— 本次工作依据的就是它**：
{该轮原话全文}
- 第 K 轮（L 字）：{开头 40 字}…
（**这是事实来源**；【任务】是这次的要求，两者冲突时**以原话为准**。
 要看哪一轮的全文就用 `read_user_turn(轮次)` 读，不要凭那几十个字猜。）

【你上次说到哪】（**这是上一轮的情况，不是本轮**）
- **判定说**已做到：{progress}
- **判定说**还缺：
  - {missing 逐条}
- **你上次说**：
{report}

【已经答复过的信息】（按先后顺序）
- 用户：{answer 1}
- 用户：{answer 2}
- 主 agent 补充：{agent_answer}
（如前后答复冲突，**以后面的为准**）

【已经产出的产物】
- {art_id}《{title}》（{N} 字）—— {summary}
（这次要**在它基础上补全**，提交完整版；不要从零重写。）

【可读产物】
- {art_id}《{title}》（{N} 字）—— {summary}
（这是清单，不是正文。要用哪一份就用 read_artifact 读全文，不要凭标题猜。）

【续跑提示】
接着已有的工作继续，**不要重复已完成的工作**。
```

**哪些段落出现，是有条件的**：

| 段落 | 出现条件 |
|---|---|
| 【任务】 | 永远 |
| **【用户原话】** | `user_turns` 非空（**首次派发也有** —— 快照在 `delegate` 建 Task 时就落下了） |
| 【你上次说到哪】 | `progress` / `missing` / `report` 任一非空 |
| 【已经答复过的信息】 | `answers` 或 `agent_answer` 非空 |
| 【已经产出的产物】 | `artifacts` 非空 |
| 【可读产物】 | `refs` 非空 |
| 【续跑提示】 | `progress` / `missing` / `report` / `answers` / `agent_answer` **任一非空** —— ⚠️ 不含 `artifacts`/`refs`（**只有重入才说"接着干"**，首跑说会让它以为有前情） |

> 🔴 **"你上次说到哪"的措辞是写死的**：判定也会看到这段 —— 不写清"这是上一轮"，
> 判定会把上一轮的"还缺"当成这一轮还没做完。
>
> 🔴 **每个 Task 字段要么渲染在这里、要么在 `evals/unit_briefing.py` 的排除名单里** ——
> 那里有断言，以后加字段忘了渲染会红。

##### 🔴 每段都标**来源**：哪句是用户说的、哪句是模型说的（2026-11-02）

**用户原话**：*"我们只需要从机制上保证数据传递正确，**标记上啥是模型交代的、啥是用户说的就行**。"*

**为什么不拦着模型加料**：主 agent 补充信息**是它的活**（它是完整的 agent，不是复读机），
平台拦不住、也不该拦。**平台能保证的是**：下游知道**每句话是谁说的** ——
这样模型补的东西**不会被当成用户的事实**。

**实测（`b3-smoke2`）**：主 agent 把用户的"人教版小学语文荷花这一课"转述成
「（叶圣陶）…小学中年级（**三年级/四年级**）」，而用户**一个字没说**；
执行器随后说「**您写的是**「三年级/四年级」，请问确认是哪个年级？」——
**把主 agent 编的算在了用户头上**。加了来源标注之后，这句话的出处就不再靠猜。

| 段 | 来源标注 |
|---|---|
| 【任务】 | （**主 agent 交代的** —— 它根据对话写的，可能补了用户没说的东西） |
| 【用户原话】 | （**用户说的** —— 平台直接搬，未经转述） |
| 【你上次说到哪】 | 子段各自标：`- **判定说**已做到/还缺：…` · `- **你上次说**：…`（**判定是第三方评审，执行器是当事人 —— 可信度不是一回事**） |
| 【已经答复过的信息】 | 逐条标：`- 用户：…` / `- 主 agent 补充：…` |
| 【已经产出的产物】【可读产物】 | 平台给的清单（**事实**，不是谁的话） |

##### 🔴【用户原话】：为什么要有它、为什么**只给清单**（2026-11-02）

**为什么要**：首次派发时，执行器看到的"任务"**100% 是主 agent 的转述**，而转述
**不只丢、还会加** —— 实测一次真实运行里主 agent 自己补了用户没说的「人教版三年级下册」
「叶圣陶的经典散文」（完整对照见 `solution.md` §9.2）。
所以平台把用户原话**硬搬**进来，让执行器手里有**事实**可对照。

**为什么只给清单（渐进式披露）**：用户消息多数很短，但**可能粘长文**（改教案那种）。
一上来全加载就把执行器的上下文塞满了 —— 所以：

| 层 | 露出什么 | 在哪 |
|---|---|---|
| **L1 清单** | 每轮一行：`第 N 轮（M 字）：{开头 40 字}…` | 交接书，**常驻** |
| **L2 全文** | 某一轮的**原话全文** | 工具 `read_user_turn(n)`（**执行器**的，闭包造，见下） |
| **L1.5 点名** | 主 agent 用 `based_on_turns=[1,3]` 点的那几轮 → **直接给全文** | 交接书（点名本身就是一次"按需取"） |

> ⚠️ **`read_user_turn` 是闭包造的**（`executor._user_turn_reader`），**不是模块级全局** ——
> 并行 delegate 时两个执行器会互相覆盖，`ARTIFACT_DIR` 那种全局状态在这儿就是坑。
>
> ⚠️ **主 agent 点名的轮次当场校验**：点了不存在的轮次 → **拒绝派发**
> （和"产物 ID 不存在"同一处理）。放行的话平台会"以为把原文给了"，实际什么都没给，
> 而执行器不知道 —— **静默降级**。
>
> 📌 **判定拿到的也是这份清单**（`briefing` 是判定输入之一）。它**没有工具循环**，读不了全文 ——
> 开头 40 字通常已含最关键约束（用户一般先说主题），够判；不够是后续可调的点。

#### 3.2.3 ⑥ 判定输入（`executor._judge`）

```
【任务要求】
{skill.instructions}

【执行者已经拿到的信息】
{briefing.strip() or '（无）'}

【实际执行结果】
---
{material}
---

【产物情况】执行者**产出了 N 个文件**。/ 执行者**没有产出任何文件**。

{_JUDGE_RULES}
```

**`material` 的取材（`_judge_material`）—— 两条路不一样，别合并**：

| | 交付物是什么 | 判定看什么 |
|---|---|---|
| **有产物** | **产物正文**（就是它） | 产物正文，**不含散文** |
| **无产物** | 只能从 AI 产出里找 | **全部轮次**的 AI 产出 |

**字符预算**（`JUDGE_ITEM_BUDGET = 4000` / `JUDGE_TOTAL_BUDGET = 16000`）：

- 每一项**各自**保头保尾（头 60% + 尾 40%），中间插**显式截断标记**：
  `……【平台截断：中间 N 字因提示词长度限制未展示给你。**你无法看到这部分**，所以不要仅凭「看不到」判未完成；但若在**看得到**的部分发现占位符、待补充、半成品，照常判未完成】……`
- 总量超 16000 → 砍掉排在后面的项，并**显式说明少了几项**

> 🔴 **标记的措辞是被实测逼出来的，别改回去**：原来写「……（此处省略 N 字）……」，
> 判定会把它当成**作者偷懒的占位符**（规则 1 说占位符不算完成）→
> **产物越长越必然判 incomplete**。而"产物长"恰恰是本架构要支持的正常场景。

#### 3.2.4 ⑧ 推回去（`executor._nudge_text`）

```
任务还没完成。

**你已经完成了**：{progress}

**还缺**：
- {missing 逐条}

请**接着往下做**，不要从头再来。

如果确实缺少必要信息才能继续，就**明确说明你需要什么**（缺哪个参数、要什么确认）；
否则不要用「已完成」之类的话收尾。
```

#### 3.2.5 ⑨ 试答（`agent._ask_main`）—— **平台强制的一步**

```
你委派出去的工作卡住了，它在请求信息。

能力：{skill_name}
你交给它的任务：{task}
执行器说：{outcome.report}
【已做到】{progress}
【还缺】
- {missing 逐条}

你的上下文（最近的对话）：
{最近 10 条消息，每条截 300 字}

请判断：**你的上下文里有没有它需要的信息？**
- 有（用户之前说过）→ can_answer=true，answer 里给出补充内容，原话照搬
- 没有 → can_answer=false，**不要编造**
```

**输出**：`_AgentAnswer{can_answer: bool, answer: str}`；失败兜底 `can_answer=False`。

#### 3.2.6 ⑩ `delegate` 的七个返回口（**可达的**）

| # | 触发 | 返回给主 agent 的 `ToolMessage` |
|---|---|---|
| 1 | **技能名不存在** | `没有能力「X」。可用：…`（**不建 Task**） |
| 2 | **产物 ID 不存在** | `产物 art_999 不存在，**这次没有派发**。本会话现有产物：…`（**不建 Task**） |
| 3 | `completed` + 有产物 | `已交付 art_001, art_002` + `\n文件：art_001 → <路径>` + `。摘要：{report 前 150 字}` |
| 4 | `completed` + 无产物（正文交付） | `已交付（成果在执行器的回复里，**请原样转达给用户，不要改写**——它可能是调用方要解析的协议块）：\n\n{report 全文}` |
| 5 | 试答成功 → 重跑 `completed` | `已交付 …（你补了信息：{answer 前 60 字}）。摘要：…` |
| 6 | `waiting_for_input`（试答后仍缺） | `⚠️ 这次没完成，它在请求信息。执行器说：{report}` + `_outcome_note` + 「平台已登记为待答…**你不用重复问，更不要自己替用户定一个答案**」 |
| 7 | `incomplete` → `failed` | `⚠️ 执行器跑了 N 轮，仍然既没完成任务、也没说明需要什么信息。` + `_outcome_note` + `它最后说：{report 前 300 字}` + 处置提示 |

`_outcome_note` = `【已做到】{progress}` + `【还缺】\n- {missing}`（**所有"没完成"的出口都带**）。

#### 3.2.7 ⑫ 答复投递（`graph._feed_waiting`）

用户下一条消息 → **原话**投给**所有** `stage=waiting_for_input` 的 Task：

```python
rec = {**t, "answers": [*(t.get("answers") or []), user]}   # ← 累积，不覆盖
outcome = run_task(skill, render_briefing(rec))
absorb_outcome(rec, outcome)
```

> 🔴 **历次答复必须累积**：执行器用完即弃，每次投递都是全新的 `run_task`。
> 只带"最新一条"的话，用户一次答一个时，**答第二个就把第一个冲掉了** ——
> 实测现象：问"用途"→答"课前预习"→问"填写形式"→答"A"→**又问一遍"用途"**。
>
> 🔴 **不做任何判断** —— 不判断"用户是不是在回答""答的是哪一条"。用户 2026-10-29：
> *"只要是答复了 就直接恢复 subagent 就行啊"* · *"答非所问就答非所问呗"*。
> 判断错了比"多问一遍"更糟。

---

## 四、软判断 / 硬判断

> 🔴 **这一节的总纲**（用户 2026-11-02）：**软判断的效果完全取决于喂给它的上下文 ——
> 所以"组织决策上下文"必须由硬机制来做；软判断只留"不得不判"的那一点，并收紧到最紧。**
>
> 换句话说：**§4.1 那几处软判断判得准不准，取决于 §三 那些交接是不是硬拼的。**
> 委托负载靠模型转述 = 转述损失 → 软判断拿到的是被加工过的世界 → 判得再好也是错的。
> 这条也是 §三 存在的理由。

**判据**：**被违反了会导致"状态不一致"的 → 硬判断（程序强制）；
只是"效果差一点"的 → 软判断（LLM 决策 / 行为建议）。**

这一节分两半，**两半都要看**：

| | 运行期（产品本体 · `proto/`） | 评测期（原型脚手架 · `evals/`） |
|---|---|---|
| **软判断** | §4.1（3 处 LLM 调用） | §4.5（3 条 rubric） |
| **硬判断** | §4.2（23 条代码保证） | §4.4（36 条代码断言） |

> 📌 **两半的判据是同一条**：能靠代码算的，不交给 LLM。
> 运行期少判一次 = 少一次抖动；评测期多断一条 = 少一个"看起来绿了"的假信号。

### 4.1 运行期的软判断（**LLM 决策**）

**平台主动发起的 LLM 调用只有三处**（其余是 agent 自己的循环）：

| # | 软判断 | 位置 | 输入 | 输出 | 失败兜底 |
|---|---|---|---|---|---|
| **S1** | **三态判定**：任务完成了没 | `executor._judge` | `instructions` + `briefing` + `material` + 产物数量 | `_Judgement{status, progress, missing, reason}` | `incomplete`（**会让它再 react 一轮**） |
| **S2** | **主 agent 试答**：你上下文里有它缺的信息吗 | `agent._ask_main` | `skill_name` + `task` + `outcome.report` + 进度/缺失项 + 最近 10 条消息 | `_AgentAnswer{can_answer, answer}` | `can_answer=False`（**交给用户，不编答案**） |
| **S3** | 主 agent 自己的决策 | `create_agent` 的模型调用 | 系统提示 + 对话历史 + 台账 + 工具定义 | 工具调用 / 直接回复 | —（原生 tool calling，无兜底） |
| **S4** | 执行器自己的决策 | 同上（执行器实例） | `_PLATFORM_RULE` + 能力指令 + 已知信息 | 工具调用 / 回复 | — |
| **S5** | 判定规则里"什么算请求信息" | `_JUDGE_RULES` 规则 3 | 同上 | 同上 | 同上 |

> 🔴 **S1 的"输入缺一不可"**：`instructions` + `briefing` + 产出 + 产物数量 ——
> **缺一个就会猜**。这个坑踩过**四次**，前三次是"忘了带某个字段"，
> 第四次是"信息在列表里、但取错了那一条"（只取最后一条 AI 消息，看不到前面几轮）。
>
> 🔴 **S2 只做一次**，靠 `delegate` 的**线性控制流**保证（`_ask_main` 单点调用，
> 其后无循环、无重入）—— **不是**靠 `agent_tried` 标记（那个只写不读，是展示字段）。

### 4.2 运行期的硬判断（**代码保证**）

| # | 硬判断 | 位置 | 防的是 |
|---|---|---|---|
| H1 | 产物必须**真实存在**且是**这次新写的** | `executor._recover_artifacts` | 编 ID；读来的算成自己交付的 |
| H2 | 执行器不许无限跑（**3 轮上限**） | `executor.MAX_REACT_ROUNDS` | 烧钱 / 死循环 |
| H3 | **状态字段由代码写** | `agent.delegate` 的 `rec.update` | 模型自报状态 |
| H4 | **三态穷举分派**（没有第四种） | `agent.delegate` | 隐式 `else` 把 incomplete 当成"在请求信息"（P3 复发） |
| H5 | 同技能 `failed` 的旧 Task → `superseded` | `agent._supersede_old` | 台账挂过期项 |
| H6 | **待答不丢**（无条件透传） | `graph.node_finalize` | 问题静默丢失 |
| H7 | LLM 调用失败 → **保守默认值** | `llm.safe_structured` | 一次抖动炸掉整轮 |
| H8 | **并行不互相覆盖** | `graph._merge_tasks`（按 id 合并的 reducer） | 并行 delegate 只剩最后一个 Task |
| H9 | 主 agent 试答**只做一次** | `agent.delegate` 线性控制流 | 反复试答 |
| H10 | **task id 不撞车** | `tool_call_id` **尾部 8 位** | 取前几位会撞（实测 `t_call_a`） |
| H11 | 试答是**强制的** | `delegate` 工具内部 | LLM 跳过这一步直接问用户 |
| H12 | **图结构固定** | `graph.build` | 流程漂移 |
| H13 | 未知技能名**直接拒绝，不建 Task** | `agent.delegate` | 挂一个跑不起来的 Task |
| H14 | 产物引用**当场校验**，不存在就拒绝派发 | `agent.delegate` | 执行器读不到会自己找补，错误埋进产物 |
| H15 | 统一返回口 `_out` **总带上被取代的旧 Task** | `agent._out` | 漏一个返回口就丢 supersede |
| H16 | `missing` **空串过滤** | `executor.run_task` | 实测模型会填 `['']` |
| H17 | 执行器 `invoke` **不传 `config`** | `executor.run_task` | 传了执行器会变成独立根图 |
| H18 | `messages` 用 `add_messages` reducer | `graph.State` | 消息被覆盖 |
| H19 | **产物 ID 发号器**用 `itertools.count()` | `artifacts._ART_SEQ` | `len+1` 是"先查后写"，并行会撞 ID → **产物静默丢失** |
| H20 | `read_skill_file` **只认清单里有的** | `skill_pack.load_skill_file` | 路径穿越 / 读二进制 / 读超大文件 |
| H21 | 技能包**只认 `SKILL.md`** | `skill_pack.ENTRY_NAME` | "这个目录是不是技能包"要靠猜 |
| H22 | **三流 UTF-8**（stdin/stdout/stderr） | `config.py` | 中文 Windows 上报错与正常输出两种编码；管道喂中文解成代理字符 → 一条坏字符整场崩 |
| H23 | 路径**只从 `config.app_root()` 算** | `config.py` / `trace.py` | 冻结后算出解包目录（`_internal/`），用户找不到、重建还会被清掉 |

> 📌 **"硬"的定义是"违反会导致状态不一致"**，不是"代码写的"。
> 比如 `_STAGE_MARK` 的文案、台账的措辞，都是代码写的但属**展示**，不在本表。

### 4.3 软判断的**规则**（提示词层）

| 规则 | 位置 | 说什么 |
|---|---|---|
| 判定三条规则 | `executor._JUDGE_RULES` | ① 需要文件产物的：逐个检查，占位/待补充/半成品不算完成 ② 不需要产物的：不能仅凭"声称完成" ③ 只有**明确请求继续所必需的信息**才算 waiting_for_input |
| 执行器平台约定 | `executor._PLATFORM_RULE` | 交付用 `write_artifact`（名称+梗概+正文）· 缺信息要**明确说明需要什么** · **答复不一定是回答你的问题，没给就再问一次** |
| 主 agent 系统提示 | `agent._MAIN_PROMPT` | 两种做法 · **不要一律问** · **产出范围以用户明确说的为准** · 传 ID 不转述正文 · **产物正文不要搬进回复** |
| 业务侧 | **skill 的 `instructions`** | "你需要知道 X"（哪些参数必答）—— **平台不猜，也不让模型猜** |

### 4.4 评测期的硬判断（`evals/checks.py` · **36 条**）

**判据**：能靠代码算的，不交给 LLM。每条签名 `(run, turn, **args) -> (ok, detail)`；
`detail` 的要求是"**失败时不用打开 JSON 就能判断是机制坏了还是断言写坏了**"。

`run` = 整场跑的记录（子进程写 JSON、父进程读回）；`turn` = 该轮结束时的快照
（`harness.snapshot()`，15 个字段，见 §5.6）。

| 组 | 断言 | 断言什么 |
|---|---|---|
| **台账** | `task_count` | `turn.tasks` 条数 == n |
| | `task_stages` | 每个 skill **至少一个** task 命中给定 stage（值可为 str 或 list） |
| | `no_task_stage` | 指定 skill 下**不允许**存在这些 stage |
| | `all_tasks_given_contain` | **每个** task 的 `task` 文本都含全部子串（验"派发前收齐信息"） |
| | `missing_excludes` | 没了结 task 的 `missing` 里不出现这些词（P1「不重复问」）—— **只扫 `missing`，不扫 `progress`/`report`**（中文子串会误伤） |
| | `missing_includes` | 至少一个没了结 task 的 `progress + missing` 里出现**全部**这些词 |
| | `every_task_has_artifact` | 每个 task 都有非空 `artifacts`；**无 task 也算失败** |
| | `artifact_no_cross_attribution` | 每个 task 记的产物必须出现在**它自己 skill 的** `run_tasks[].produced` 里（不许把读来的算产出） |
| **委派与兜底** | `delegate_calls_max` / `delegate_calls_exact` | 到该轮为止的 `delegate` 调用数 ≤ n / == n |
| | `platform_relayed` | 平台把待答透传给了用户（日志里出现 `finalize: 透传`） |
| | `no_platform_relay` | 不该有透传 |
| | `platform_fed_answer` | 平台把用户原话投给待答任务（日志含 `把用户这条消息**原话**投过去`） |
| | `finalize_rescued` / `no_finalize_rescue` | 旧名保留、语义已变：**直接转发** `platform_relayed` / `no_platform_relay` |
| **隔离与体积** | `main_agent_no_artifact_read` | 主 agent 读产物正文次数 ≤ `allow`（默认 0），以工具名 `read_artifact` 判 |
| | `main_ctx_under` | 主上下文体积 < n（口径**不含 system prompt**） |
| | `reply_chars_under` | 回复字数 < n |
| | `artifact_chars_over` | 产物库正文总量 > n（**隔离必须用长内容才测得出**） |
| | `artifact_count` | 产物个数 == n |
| **产物正文** | `artifact_no_placeholder` | 产物正文不含作者写的占位符（默认 7 词：`待补充/待填写/待完善/此处省略/TODO/TBD/占位`，**刻意不收单字「略」**） |
| | `artifact_html_complete` | **声明为 html** 的产物必须"有头有尾"（§4.4.1） |
| | `artifact_titles_contain` | 每个子串至少在**一个**产物标题里出现 |
| | `task_refs_contain` | 指定技能的 task 必须存在且带产物引用 `refs` |
| **写入流水 / 异常 / 回复** | `artifact_write_no_collision` | 每次 `store_artifact` 拿到唯一 ID（不许覆盖） |
| | `artifact_writes_multithreaded` | 🔬 写入来自 >1 个线程（否则"没撞车"是假阴性） |
| | `artifact_writes_timing` | 🔬 写入最小时间间隔 ≤ `max_gap_s`（**真并发**，不是只数线程） |
| | `no_exception` | 整轮没炸（`run["error"]` 为空） |
| | `fault_fired` | 🔬 注入的故障**真发生**了 |
| | `run_task_count_over` | 🔬 该技能被跑次数 ≥ n（首跑 + 每次投递各算一次） |
| | `llm_calls_under` | LLM 调用总数 ≤ n |
| | `reply_contains` / `reply_excludes` / `reply_nonempty` | 回复含 / 不含这些子串；非空 |
| | `no_duplicate_completed` | 同一技能最多一个 `completed` task（**不能用 `delegate_calls_max` 代替** —— 补救性重派是合法的） |
| | `supersede_worked` | 重新委派同技能后 `superseded >= 1` 且 `waiting_for_input == 0` |

**🔬 标记的是"测试有效性"断言** —— 它们断的不是产品行为，而是"**这条 case 真的压到了那条路径吗**"。
`report.py` 里 `VALIDITY_CHECKS = {"artifact_writes_multithreaded", "fault_fired"}`，
附注写着：**这条不绿，同 case 其他断言的通过是假阴性**。

> ⚠️ **已注册但当前没有 case 引用**的 9 条：
> `artifact_writes_timing` · `missing_includes` · `main_agent_no_artifact_read` · `main_ctx_under` ·
> `artifact_write_no_collision` · `artifact_writes_multithreaded` · `llm_calls_under` ·
> `reply_excludes` · `supersede_worked`。
> 其中 `artifact_write_no_collision` 端到端**测不到**（竞态窗口微秒级、真实写入间隔 4–205 秒），
> 改由 `evals/unit_artifact_race.py` 用 Barrier 压 —— **记档，不假装它被覆盖了**。

#### 4.4.1 `artifact_html_complete` 的"**不按内容嗅探**"规则

```python
html_ids = [aid for aid, a in arts.items() if file_ext(a.get("type") or "") == ".html"]
# 判据：剥掉开头空白与 HTML 注释后，startswith("<!doctype html") or startswith("<html")
#       且 "</html>" in s
```

- **类型以声明为准**：`type` 由产出方在交付时一起说（`write_artifact(file_type=...)`），
  平台据此**硬性派发**扩展名（同一张表 `proto.artifacts.FILE_TYPES`）。
- **一份 html 都没声明 → 失败**（不是跳过）。
- **剥开头注释是实测踩出来的**：`t2-refs5` 里设计说明有 36 行，`<!DOCTYPE html>` 落在 1000 字开外，
  被误判成"不是从文档头开始"。

> 🔴 用户 2026-10-30：*"就是不用嗅探呀 测试也不用"*。
> 不是"看着像 HTML 就算网页"，而是"**你说了是网页，那就得是完整的网页**"。

### 4.5 评测期的软判断（`evals/judge.py` · **3 条 rubric**）

**判据**：只有**必须语义判断**的才走这里；**评分调用失败时判「不通过」，不判「通过」**。
在**父进程**跑（子进程已退出），输入是子进程录下的快照。

| rubric | 断言什么 | 喂给它的材料 | 输出 |
|---|---|---|---|
| `no_repeat_question` | 主 agent / 执行器有没有向用户索要**用户其实已经说过的**字段 | ① 到该轮为止的**全部用户原话** ② 该轮回复 ③ 每个 task 的 `report`（**截 300 字**）④ 待查字段清单 | `asked: list[str]`（非空 → 失败） |
| `artifact_derives_from_source` | 后序产物**内容上**是否真的基于源产物（依赖成立的语义证据） | 源产物正文（**截 2500 字**）+ 每份其他产物（**截 1500 字**）+ 各自的标题 | `derived: list[str]` / `generic: list[str]` / `evidence: str` |
| `reply_is_summary` | 产物正文有没有被搬进主 agent 的回复（隔离的另一面） | 产物清单（ID + 字数）+ 该轮回复 | `pasted_body: bool`（true → 失败） |

> 🔴 **"助手内部向它自己索要、但没转达给用户"不算**（`no_repeat_question` 的判据里写死了这条）——
> 否则试答那一步会被误判成"又问了一遍"。

**留档（判错归因）**：每条 rubric 的**逐字 prompt 与 response** 追加进
`runs/<tag>/artifacts/<case>__r<rep>/_run.md` 的 `## 软判断 · \`{name}\`` 一节，
带一句固定的话：**"判错了先看这里：是**模型判错**，还是**喂给它的材料不对**。"**

### 4.6 两者怎么汇总

每条断言在结果里是一个 dict，字段固定 5 个：`check` / `at` / `soft` / `pass` / `detail`。

| | 硬判断 | 软判断 |
|---|---|---|
| 名字 | 函数名去掉 `check_` | 函数名去掉 `rubric_` |
| `soft` | `False` | `True` |
| 跑在哪 | 父进程（子进程退出后） | 同左 |

- **run 级**：`green = 所有断言 pass`；`rate = green / run 数`。
- **硬软同权** —— 没有任何一处对 `soft` 降权。
- `report.md` 四节：① 通过率总表（低→高）② 逐条断言通过率（**不是 100% 的即 flaky**）
  ③ 指标（中位数/最大）④ 失败详情。
- 类型列有三种：`🔬 测试有效性` / `软断言` / `硬断言`。

> 📌 **报告头写明**：每个 run 的完整记录在 `artifacts/<case>__r<rep>/_run.md`，
> 且 **"判「完整性」以产物原文为准，不以判定的话为准"** ——
> 判定被预算截断时给的"完成"是**推测**（§3.2.3）。

### 4.7 `_run.md` 的四个 section（**一次跑完，一份读完**）

| 顺序 | section | 谁写 |
|---|---|---|
| ① | 概览 + **逐轮全记录**（用户输入 / 主 agent 输出 / 工具调用完整参数 / 工具返回 / 平台注入的台账 / Task 台账含产出与输入引用 / 执行器原话） | 子进程 `harness.dump_run_record` |
| ② | **软判断留档**（逐字 prompt 与 response） | 父进程 `judge._log` 追加（**只在跑批路径**；交互式没有这一节） |
| ③ | **原始 trace 附录** | `evals.run._append_trace` 追加 |

> 🔴 **"交接内容"在 ① 里是被"拆开"渲染的**：`delegate` 的 `ToolMessage` 不是整段贴，
> 而是按 `_RECEIPT_SPLIT` 的 8 个标记切成
> **「平台的框架话」/「执行器的原话」/「平台附注 · 已做到什么程度 / 还缺什么」/「平台给主 agent 的指示」** ——
> 读记录的人一眼能分清**哪句话是谁说的**。

---

## 五、字段字典

### 5.1 `Task`（`state["tasks"]` 里一条 · **14 个字段**）

| 字段 | 谁写 | 含义 |
|---|---|---|
| `id` | `delegate` | `t{tool_call_id 尾部 8 位}` |
| `skill` | `delegate` | 能力名 |
| `task` | `delegate`（**主 agent 给的原话**） | 这件工作的具体要求 |
| `stage` | `delegate` / `_feed_waiting` | 5 态（§6.2） |
| `artifacts` | `absorb_outcome` / `delegate` | **产出**的产物 ID 列表（只在非空时覆盖） |
| `refs` | `delegate` | **输入引用**的产物 ID 列表（⚠️ 与 `artifacts` 方向相反） |
| `report` | `absorb_outcome` | 执行器最后说的话 |
| `progress` | `absorb_outcome` | 判定给的"已经做到什么程度" |
| `missing` | `absorb_outcome` | 判定给的"还缺什么" |
| `answers` | `_feed_waiting` | **历次用户答复**（累积，不覆盖） |
| `agent_answer` | `delegate` | 主 agent 试答补的信息 |
| `agent_tried` | `delegate` | 试答过没（⚠️ **只写不读**，展示字段） |
| `user_turns` | `delegate` | **用户原话快照** `[{"n": 1, "text": "…"}]` —— 建 Task 那一刻落的，**不是渲染时现取** |
| `based_on` | `delegate` | 主 agent **点名**依据哪几轮（平台据此把那几轮原文直接给执行器） |

### 5.2 `ExecOutcome`（执行器 → 平台）

```python
status: Literal["completed", "waiting_for_input", "incomplete"]  # = "incomplete"
artifacts: list[str]   # 这次新写的产物 ID
report: str            # 最后一条有内容的 AI 消息
rounds: int            # 实际 react 了几轮
progress: str          # 判定给的：已经做到什么程度
missing: list[str]     # 判定给的：还缺什么
```

### 5.3 `_Judgement`（判定的输出 · 结构化）

```python
status: Literal["completed", "waiting_for_input", "incomplete"]
progress: str        # 非 completed 时填
missing: list[str]   # incomplete 时填
reason: str          # 一句话理由 —— **只写日志**，供事后复盘误判
```

### 5.4 `Artifact`（产物库里一条 · **7 个字段**）

| 字段 | 例 | 谁给 |
|---|---|---|
| `title` | `荷花-教案` | 产出方 |
| `summary` | `人教版三年级上册第一课时教案` | 产出方（**对外露出的那一层**） |
| `content` | （正文） | 产出方 |
| `type` | `md` / `html` | **产出方声明** |
| `ext` | `.md` / `.html` | 平台**硬性派发**（`FILE_TYPES` 一张表） |
| `file` | `art_001_荷花-教案.md` | 平台（标题 → 文件名安全的一段） |
| `path` | `C:\…\art_001_荷花-教案.md` | 平台（**设了 `ARTIFACT_DIR` 才有**） |

### 5.5 `State` / `AgentState`

```python
class State(TypedDict):          # 父图
    messages: Annotated[list[AnyMessage], add_messages]
    tasks:    NotRequired[Annotated[list[dict], _merge_tasks]]
    log:      NotRequired[Annotated[list[str], operator.add]]

class AgentState(TypedDict):     # 主 agent 子图 —— **故意不含 log**
    messages: Annotated[list[AnyMessage], add_messages]
    tasks:    NotRequired[Annotated[list[dict], _merge_tasks]]
```

> 🔴 **子图状态必须窄**：子图会把它状态里的字段**原样回吐**给父图，
> 父图的 `log` reducer 是 `operator.add` —— 于是父图自己的 log 被子图又加了一遍，
> **每轮翻一倍**（实测 `1 → 5 → 12 → 27`）。`tasks` 不受影响（按 id 合并，幂等）。

### 5.6 `turn` 快照（`harness.snapshot()` · **15 个字段**）

**这是"跑批"与"交互式"共用同一种记录格式的落点** —— 两个入口都调它。

| 字段 | 来源 |
|---|---|
| `turn` / `user` / `wall_s` | 入参 |
| `input_source` | 默认 `"scripted"`，**交互式覆盖成 `"human"`**（`_run.md` 的标题按它分叉） |
| `reply` | 最后一条有内容的 `AIMessage` |
| `tasks` | `state["tasks"]`（**12 个字段全带上**） |
| `main_ctx_chars` | 消息 content + tool_call args 长度（**不含 system prompt**） |
| `sys_prompt_chars` | `_MAIN_PROMPT` + 能力清单的长度（**不在 state 里**，故单记） |
| `artifacts_chars` / `artifacts` | 产物库正文总量 / `{aid: 字数}` |
| `delegate_calls` / `tool_calls` | 本轮的工具调用（名字 / 名字+参数摘要） |
| `messages` / `new_msg_kinds` | 本轮全部新消息（**原文不截断**） |
| `log` | `state["log"]` |

**落盘**：`runs/<tag>/` 下 `_console.log`（终端全文，去色）· `artifacts/<tag>__r1/`
（产物原文 + `_index.md` + `_run.md`）· `results.json`（顶层 14 个键）。

> ⚠️ **交互式与跑批的 `results.json` 有三个差异**：交互式**没有**
> `per_call_tokens` / `checks` / `child_stderr`，**多**一个 `skills_dir`。
> `_run.md` 的「软判断留档」那一节**只在跑批路径存在**。

---

## 六、图与生命周期

### 6.1 图结构

```
START → prepare → agent → finalize → END        （compile(checkpointer=InMemorySaver())）
```

### 6.2 `Task.stage`（5 态）

```
                delegate 建 Task
                      │
                      ▼
              ┌───────────────┐
              │   running     │
              └───────┬───────┘
                      │
        ┌─────────────┼─────────────┐
   completed   waiting_for_input   failed
   （交付）      （等用户答复）      （推满 3 轮仍 incomplete）

   另有 superseded：同 skill 的 failed 旧 Task 被重新委派时自动标记（**不用管了**）
```

| 状态 | 台账里 | 谁能解除它 |
|---|---|---|
| `running` | 一行 | 同一轮内即变（同步委派） |
| `completed` | 一行（含产物 ID） | — |
| `waiting_for_input` | **展开**：进度 / 缺失项 / 执行器原话 | **用户答复**（`_feed_waiting` 投递） |
| `failed` | **展开**：同上 | 主 agent 重新委派（旧的 → superseded） |
| `superseded` | 一行 | — |

**与 `_Judgement.status` 的关系**：前三个**同名**，唯一映射 `incomplete → failed`。

**暂未实现的三个**（记清了为什么不做、什么时候该做）：

| 状态 | 为什么现在不做 | 什么时候该做 |
|---|---|---|
| `pending` | 建 Task 就立刻派发，没有"创建了还没跑"的窗口 | 做「计划-执行分离」时 |
| `waiting_for_dependency` | `delegate` 是**同步**的，依赖在主 agent 循环里天然解决 | 做「异步委派」时 |
| `cancelled` | 要给主 agent 新工具，场景**尚未实测到** | 真遇到时 |

### 6.3 四条语义区分（实现时的防错清单）

| 不要混 | 说明 |
|---|---|
| `waiting_for_input` **≠** Graph 运行失败 | 前者是**正常状态** —— 图跑得好好的 |
| `completed` **≠** 某次模型调用正常结束 | 模型正常返回 ≠ 任务完成 |
| `failed` **≠** 模型返回了错误字符串 | 它的定义是"推满 3 轮仍**既没完成、也没请求信息**" |
| **某个 Sub Agent 执行完成 ≠ Task 已经完成** | 执行器"跑完了"和"干完了"是两回事 |

---

## 七、一次完整运行（真实模型实测）

```
用户：帮我备课
  │
  ├─ prepare   无台账（首次）→ 不注入
  ├─ agent     信息太少 → 先问（**Task = 0**）
  └─ finalize  无待答 → 不补

用户：分数的初步认识
  │
  ├─ agent     🧠 调 delegate(lesson-prep) → 建 Task（stage=running）
  │             ↓ 交接书：【任务】分数的初步认识（无【上次说到哪】—— 首跑）
  │           执行器读能力指令 → 发现缺「课时」→ 判定 waiting_for_input
  │             ↓ 平台强制试答 → 答不上
  │           stage = waiting_for_input，台账里展开"还缺：课时"
  ├─ finalize  透传执行器原话 → 用户看到问题
  │
用户：第1课时
  │
  ├─ prepare   _feed_waiting：原话投给待答 Task（answers 累积 1 条）
  │            → 执行器这次拿到课时 → completed（art_001 落盘）
  │            → 台账注入：「t…「lesson-prep」✅ 已完成 → art_001」
  ├─ agent     看到台账（不用重派）→ 回复"备课搞定了…文件：art_001 → C:\…"
  └─ finalize  无待答 → 不补
```

---

## 八、与最初版本的对比

| | 最初（workflow 版） | **现在** |
|---|---|---|
| 主 agent | 纯路由器（只有 `llm_pick` + 试答） | **完整的 agent**（6 个工具 + 循环） |
| 流程 | 代码写死的固定边 | **由 LLM 驱动**（图只有三个节点） |
| 闲聊"你好" | ❌ 返回"（无产出）" | ✅ 自己答 |
| 建 Task 时机 | 选完技能**必建** | **决定委派才建** |
| 完成判定 | `ExecResult.status`（执行器**声称**） | **三态判定**（问"任务完成了没"） |
| 交付物声明 | `deliverables` 清单 + `required` | **不需要** |
| 参数 | 动态 schema 抽取 + 并集计算 | **不抽取**，原话原样带进去 |
| 挂起 | `interrupt()` + `Command(resume)` | **不需要**（`waiting_for_input` 由 Task 推导） |
| 问参数 | 规划阶段提前问 | **运行时**：先问主 agent，再问用户 |
| 谁把问题交给用户 | 主 agent 自己转述 | **平台无条件透传**（2026-10-29 改） |
| 恢复待答 | 主 agent 重新 delegate | **平台投递用户原话**（2026-10-29 改） |
| Task 状态 | 无 | **5 态**（含 `superseded`） |
| `Skill` 字段 | name/description/instructions/**params**/**criteria** | **只剩三个**（+ 技能包文件夹） |
| 技能形态 | 单个 `.md` | **文件夹**（`SKILL.md` + 任意参考资料，渐进式披露三层） |
| 产物 | 内存 dict，跑完统一落盘 | **当场落盘**（返回里带真实路径） |
| 决策输出 | 混着（工具参数 / 自然语言） | **一律 `with_structured_output`** |
| 可观测 | 跑完才 dump | **逐 super-step + 逐字流式**（黑屏 0%） |
