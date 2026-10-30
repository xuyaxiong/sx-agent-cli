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

`artifact_no_placeholder` / `artifact_looks_like_html` / `artifact_chars_over` 这几条
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
