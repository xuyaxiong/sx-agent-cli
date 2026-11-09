# evals —— 跑批回归

> `docs/scenarios.md` 是**人读的场景集**（每条有"为什么重要"）；
> 这里是它的**可执行版本** —— 跑 N 遍、机器判、出通过率。
> 两份要对着看：改了场景，两边都改。

## 为什么要有它

`scenarios.md` 是**单次采样**：每条场景手工跑 1 次，人读日志判对错。
那测的是"**能不能做到**"，不是"**稳不稳**"。

稳定性 = **重复跑出来的方差**。项目自己已经踩过这个坑：
`debug_judge_variants` 的分数重跑后从「A 3/6、C 4/6、E 6/6」变成「全是 5/6」，
文档里写了"**别拿单个数字当论据**"。

所以这里补三件事：**可机判**（否则场景数量上不去）、**可重复**（否则测不出稳定性）、
**有量纲**（否则"效果"没有数字）。

## 跑

```bash
uv run python -m evals.run --tag baseline              # 全部 case × 3 遍
uv run python -m evals.run --tag smoke --tier smoke    # 只跑 smoke 档
uv run python -m evals.run --tag x --only s01-multi-skill-dedup --reps 5
uv run python -m evals.report baseline                 # 只重出报告
uv run python -m evals.unit_artifact_race              # 单元：产物 ID 竞态（不调 LLM）
uv run python -m evals.unit_frozen_paths               # 单元：**冻结后的目录约定**（不调 LLM、不打 exe）
```

**实测发现**（F1–F6）记在 `../docs/discussion-log.md`。

结果落 `evals/runs/<tag>/`：

| 文件 | 给谁看 | 内容 |
|---|---|---|
| `artifacts/<case>__r<rep>/_run.md` | **人**（一份读完） | 逐轮全记录（用户输入 / 主 agent 输出 / 工具调用完整参数 / 工具返回 / 台账）→ 软判断留档（逐字 prompt 与 response）→ 原始 trace 附录 |
| `artifacts/<case>__r<rep>/` 其余 | 人 | 每个产物一个文件（HTML 原样落 `.html`，可直接打开）+ `_index.md` |
| `results.json` | 人 + 工具 | 整批结果（**缩进的多行 JSON**，不是 JSONL —— 一行几十万字没人打得开） |
| `report.md` | 人 | 通过率总表 / 逐条断言 / 失败详情 |
**tag 就是基线名** —— 拿两个 tag 的报告对比，就是"这次改动弄坏了哪条"。

## case 格式

```yaml
id: s01-multi-skill-dedup
family: 痛点                  # 归类用
why: "P1 —— 用户已经说过的信息不该再问一遍"    # 挂到真实痛点，否则后人不知道能不能删
realism: 文档记录的需求        # 实测发生过 / 文档记录的需求 / 推测
tier: smoke                  # smoke（便宜快）/ full
skills:                      # 可选：case 自带的技能集
  - name: full-lesson-plan
    description: ...
    instructions: ...
skills_mode: extend          # extend（默认，追加到内置 4 个）/ replace
fault:                       # 可选：故障注入
  structured: 2              # 让第 1 次结构化调用的两次尝试都失败
turns:                       # 用户逐轮说的话
  - "帮我备课、导学单，再出个随堂小测，三年级数学"
checks:                      # 硬断言（代码算）
  - check: task_count
    at: 2                    # 可选：第几轮结束时判（省略 = 最后一轮）
    n: 3
soft:                        # 软断言（LLM 评分）—— 只有必须语义判断的才放这
  - check: no_repeat_question
    fields: ["年级", "学科"]
```

### `realism` 三档（拿不到真实语料时的诚实标注）

| 值 | 含义 |
|---|---|
| **实测发生过** | 项目日志/文档里真实出现过的行为或故障 |
| **文档记录的需求** | 用户明确提过的业务场景 |
| **推测** | 按教育域特征编的 —— **第一轮上线后要用真实日志回补** |

## 断言

硬断言在 `checks.py`，软断言在 `judge.py`。
**能靠代码算的绝不交给 LLM**（设计原则 ③）；中文子串必然误伤的地方（「三年级」含「年级」）才走评分。

跑 `uv run python -c "from evals.checks import CHECKS; print(sorted(CHECKS))"` 看全部可用断言。

### 🔬 测试有效性断言

`artifact_writes_multithreaded` 和 `fault_fired` 这两条**不是在测平台，是在测这次跑有没有测到东西**：

- 并发没被触发 → 同 case 里"产物没撞车"这条**通过是假阴性**
- 注入没生效 → "抖动没炸掉 turn"同样无意义

报告里它们单独标出来。**这两条不绿，同 case 其他断言的通过都不算数。**

## 执行路径开关（`exec_path`）—— **平台造路**

```yaml
# case 里（evals/cases/*.yaml）
exec_path: auto        # 默认：两条路都开，由模型自己选（**产品行为**）
exec_path: delegate    # 强制委派
exec_path: inline      # 强制主 agent 自己干
```

```bash
# 交互式入口也一样能用（2026-11-02 接上）—— 想手工看某条链路时用它
uv run python -m app.chat --skills skills --exec-path delegate
```

**为什么要它**：跑批时想验某条链路，原来只能**碰运气**碰上一个模型愿意走那条路的 case ——
而"走哪条路"是**模型的自由选择**，于是那条断言必然脆（判据见 `checks.py` 顶部）。
有了开关，**测哪条就开哪条**，不用再"妄图正好有一条符合路径的 case"。

| 档 | 怎么实现 | 硬度 |
|---|---|---|
| `inline` | **不给 `delegate`** → 工具不存在，它调不了 | **全硬** |
| `delegate` | 裁掉"自己干"的工具（`write_artifact` / `read_skill` / `read_skill_file`）→ 拿不到原料 | **半硬** —— 挡不住它在回复里硬写；所以配了句系统提示，case 里再用 `task_count_at_least` 盯着"到底派没派" |
| `auto` | 现状 | — |

> 🔴 **它第一次跑就逼出一个真 bug**（见 `../docs/discussion-log.md` **F23**）：
> 任务书里只给了技能自带文件的**路径**、没给**技能名**，而
> `read_skill_file(skill_name, path)` 的**第一个参数就是技能名** —— 执行器只能猜，
> 猜不中就永远读不到（实测试了 4 个变体、卡 121 秒，最后反过来问用户"技能叫什么"）。
> **这个 bug 在 `auto` 模式下碰不到** —— 详见 F23。

## 实现上的几个坑（踩过，记下来）

**① 每个 run 必须独立子进程。** 不是洁癖：

| 状态 | 存在哪 | 不隔离的后果 |
|---|---|---|
| `skills.ARTIFACTS` | **模块级全局 dict** | 上轮产物带进下轮，ID 递增、`_recover_artifacts` 扫描范围越来越大 |
| `trace.LOG_FILE` | **进程级**，导入时开文件 | 所有 run 混进同一个日志 |
| `config.get_model` | `lru_cache` | 跨 run 复用同一个模型实例 |

顺带白拿并发和崩溃隔离。日志文件名只到**秒**，并发子进程会撞 —— 用 `PROTO_LOG_TAG` 区分
（为此给 `proto/trace.py` 加了一个可选环境变量，不设时行为不变）。

**② 三个"值导入"的坑。** `agent.py` / `executor.py` / `llm.py` 都是
`from proto.config import get_model` 这种**值导入** —— 改 `proto.config.get_model` **不生效**，
得改**它们各自命名空间里的那个名字**。`SKILLS` 同理（`proto.agent.SKILLS` 一处就够，
因为 `delegate` 和 `build_agent` 都读 `proto.agent` 的模块全局）。

**③ 故障注入要注在 `safe_structured` 内部那一步**（`get_model().with_structured_output(...)`），
这样测的才是它**自己的重试 + 保守默认值**兜底，而不是绕过它。

**④ 评分调用失败判「不通过」，不判「通过」。** 判不了 ≠ 合格。

**⑤ `results.json` 里带着**产物正文全文**（`artifact_final[].content`）。**

`artifact_no_placeholder` / `artifact_html_complete` / `artifact_chars_over` 这几条
**纯代码**断言要扫正文才判得了，所以只能把正文一起回传（一次跑几十 KB，和产物原文重复）。

要清的话得改成**在子进程里先算结论**（只回传 pass/fail + 详情），
断言就不用拿正文了。**没定要不要做**（2026-10-29 用户问过，未决）。

## 与 `scenarios.md` 的关系

| | `docs/scenarios.md` | `evals/` |
|---|---|---|
| 读者 | 人 | 机器 |
| 内容 | 场景 + 为什么重要 + 状态 | 可执行断言 |
| 采样 | 1 次 | N 次，出通过率 |
| 判读 | 人读日志 | 代码 + 少量 LLM 评分 |

**改了场景，两边都改。** `scenarios.md` 里"为什么重要"那段，就是 case 里的 `why:`。

## 文件分工（2026-10-30 拆过）

| 文件 | 是什么 | 谁用 |
|---|---|---|
| **`harness.py`** | **装配 + 记录层**：技能加载 / `install` / `snapshot` / 落盘 / 路径约定 | **交互式 CLI 与跑批共用** |
| `_child.py` | **跑批子进程**：一个 case 的一遍 → JSON（`run.py` 用 `-m` 起它） | 只有跑批 |
| `chat.py` | 交互式入口（你当用户） | 只有人 |
| `run.py` / `report.py` / `checks.py` / `judge.py` | 跑批的调度 / 报告 / 断言 / 评分 | 只有跑批 |
| `unit_*.py` | 不调 LLM 的单元测试（竞态 / 交接书 / 投递 / **冻结路径**） | 秒级，改完先跑它们 |

> 🔴 **`unit_frozen_paths.py` 是"打包才走的那条分支"的常驻守卫** ——
> 冻结后的路径算错，**开发态永远碰不到**，而跑一次打包要几分钟、用户还得干等。
> 它用 `sys.frozen=True` + 假 exe 目录把那条分支走一遍，**秒级**。
> 已经拦下两次（示例技能铺错、trace 附录写进 `_internal/`）。
> **真打包仍然不可替代**（验"冻结本身"：漏收依赖、`_MEIPASS`、双击启动），
> 但那是**攒着一起打**，不是改一处打一次。

> 🔴 **为什么要拆**：原来这些都挤在 `_child.py`（803 行）里，其中只有 68 行是跑批独有的，
> 其余全是共用的 —— 但文件名只描述了那 68 行，于是 `chat.py` 的
> `from evals._child import ...` 看着像"产品依赖测试代码"。**共享是故意的**（两个入口要产
> **同一种记录格式**，否则收回来的一堆 run 没法比），错的是名字。
>
> ⚠️ 路径也统一收在 `harness.py`（`ROOT` / `RUNS_DIR` / `DEFAULT_SKILLS_DIR`）——
> 打包成 exe 后它们是 **exe 旁边的目录**，开发时还是仓库里的。**别在别处自己算路径。**
