# 实验计划 · 用真实复杂技能验证原型的多 Skill 执行与交付

> 被测：`proto/`（当前 HEAD）。
> 输入技能：`sx-agent/test_data/skills` 里那批"真实复杂度"的技能包
> （长指令 + `references/` + 多步流程 + 明确的交付要求），而不是原型自带的 4 个 150 字小技能。
> 目标场景（用户原话）：**某一课 → 先生成教案，再生成导学单和交互式教案**。

---

## 一、验什么

| # | 能力 | 判定问题 |
|---|---|---|
| 1 | **多 Skill 执行** | 一条链上的多个技能能不能都被委派、都完成？**后序技能真的吃到前序产物吗？** |
| 2 | **交付** | 长产物完整落库（无占位符）、归属不串台、正文不进主上下文、产物可取回可审计 |

不在本轮：多媒体（图/音/视频）、参数 schema、参考文件机制、脚本技能。

## 二、被测技能与适配（本计划的关键决策）

原型 `Skill` = `name + description + instructions` 三样，没有参数、没有交付声明、
没有参考文件机制。测试技能是 `.md` 包。中间必须有一次**机械适配**，规则固定、可审计：

| 规则 | 做什么 | 为什么 |
|---|---|---|
| ① description | 原样取 frontmatter 的 `description` | — |
| ② 交付通道换名 | `create_artifact` → `write_artifact` | 原型唯一的交付工具 |
| ③ 删掉引用不存在工具的步骤 | `get_current_time` / `query_knowledge` / `fetch_url` / `generate_image` / `generate_audio` / `request_user_input` / `glob` / `read` | 原型没有这些工具，留着只会让执行器对着空气使劲 |
| ④ `references/` 内联为指令附录 | lesson-prep 3 个（约 3.2k 字）、guided-learning 1 个（约 1.5k 字） | 原型没有参考文件机制；这些文件本来就是"指令的一部分"，不内联等于把技能掏空 |
| ⑤ 提问措辞 | "必须用 request_user_input 提问，不要写在正文里" → "直接向用户提问" | **唯一一处改语义**（技能写的是别的平台的契约），要记档 |
| ⑥ 去模板占位符 | `{{topic}}` / `{{instruction}}` / `{{attachments}}` 等 → 自然语言（"由任务给出；没给就问用户，不要自己编"） | 原型没有参数模板机制 |

**其余一律不动**：格式要求、工作流程步骤、"缺信息就问"的约定，原样保留 —— 那正是要测的。

**已落地**（2026-10-29）：副本 + 适配版在 `evals/skills/` —— `source/` 逐字（md5 已核），
`adapted/` 为本项目口径，两边 diff 即可逐行审计。规则与逐技能改动清单见 `evals/skills/README.md`。

**入选技能**

| 技能 | 角色 | 适配后特征 |
|---|---|---|
| `lesson-prep` | 链的起点 | 3 个 references 内联（约 5k 字指令）、2 问确认、要求完整教案 |
| `guided-learning` | 链的第二环 | 1 个 references 内联、2 问确认；**技能里不含"基于教案"** —— 依赖由用户原话提，见下 |
| `interactive-webpage` | 链的第三环 | 短指令、单文件 HTML 硬约束（无外部资源、可重置、aria-live） |
| ~~`exam-grading`~~ | 跳过 | 要答题卡图片（多媒体输入） |
| ~~`primary-english-dialogue`~~ | 跳过 | 要图 + 音频 |
| ~~`text-analyzer`~~ | 跳过 | 靠 `scripts/` 防编数，原型无脚本机制，适配后失去意义 |

## 二·补 首轮实测后的机制变更（2026-10-29）

首轮跑完（`--tag t2-probe`，12/13 绿）暴露两个问题，用户拍板后改了 proto：

**问题一 · 跨技能依赖靠主 agent 转述**：它 `read_artifact` 把 5162 字教案读进自己的上下文
（1369 → 9322 字，`main_ctx_under` 红），再提炼成要点抄进两个 task。转述既失真又破隔离。
→ **改成结构化传递**：`delegate(skill_name, task, artifacts=["art_001"])`；平台当场校验 ID
（编的 ID 直接拒 + 给真实清单），只把**清单**（ID/名称/字数/梗概）注入执行器，正文由执行器
自己 `read_artifact` 读。依赖因此变成**可硬断言**的（`task_refs_contain`）。

**问题二 · 主 agent 看不到技能原文却要写 task**：它在 task 里发明了"控制在一页A4以内"
（用户没说、技能里没有、教案里也没有），压过了技能的 7 板块模板，交付缩水成 3 板块。
→ **渐进式披露补第二层**：主 agent 新增 `read_skill(name)`（按需读技能全文，写 task 前可核对）
与 `list_artifacts()`（产物清单）。用户原则：**主 agent 不是包工头，它也要能执行、能按需读**。

**产物侧对称**：`write_artifact(title, summary, content)` —— 梗概由产出方交付时一起给，
它是产物对外露出的那一层。`write_artifact`/`read_artifact`/`list_artifacts`/`read_skill`
构成与技能侧同一套渐进式披露。

顺带修了两处**早已失效**的离线测试（与本次改动无关，预算 800→4000 时语料没跟着放大）：
`experiments/test_judge_material.py`（① 与 ④ 段语料短于预算 → 假失败）与
`evals/unit_artifact_race.py` 对照组的签名。

## 三、场景矩阵

| case | 场景 | 验的是 |
|---|---|---|
| **t1-chain** | 跨轮三技能链（主场景） | 多技能串行 + **跨技能依赖是否成立** + 三种交付 |
| **t2-same-turn** | 一条消息要三样 | 同轮多技能并发委派、长产物并发写不撞车 |
| t3-long-delivery（可选） | 单技能出长教案 | 长产物完整性、判定不误伤（F1 回归） |
| t4-foreign-skill（可选） | 原样投喂未适配技能包 | 平台对"外来契约"技能的容忍度（附录发现） |

### t1-chain（主场景）

```yaml
skills_mode: replace        # 只挂这三个，别混进自带的 quiz / courseware-config
turns:
  - "帮我给三年级语文《荷花》备一节课，出教案"
  - "基于刚才那份教案，出一份配套的学生导学单，环节要对得上"
  - "再把这份教案做成一个学生能点着学的交互网页"
```

断言：
- 硬：三个 task 全 `completed`、每个 task 都有自己的产物、产物数 = 3、无串台
  （`artifact_no_cross_attribution`）、总字数下限（`artifact_chars_over`）、
  `artifact_no_placeholder`、主上下文上限（`main_ctx_under`）、回复字数上限（`reply_chars_under`）
- 软：**依赖桥接**（第 2/3 轮 delegate 的 `task` 文本里带没带前序产物 —— ID 或内容要点）、
  **内容衍生**（导学单/网页是不是真的基于那份教案，而不是一份通用件）
- 反例判据：后序产物与教案不同课题 / 环节对不上 → 依赖不成立

### t2-same-turn

```yaml
turns:
  - "给三年级语文《荷花》出一份教案、配套导学单，再做个交互网页"
```

断言：`task_count: 3`、全 completed、`every_task_has_artifact`、`artifact_count: 3`、
`artifact_write_no_collision`、`artifact_writes_multithreaded`（**有效性断言** —— 没触发并发
则"没撞车"是假阴性，报告里单独标）。

## 四、harness 需要的改动（proto 不动）

> 原则：**不改 `proto/`**，否则这轮结果和已有 baseline 不可比。

1. `_child.py`：支持 `skills_dir:`（case 引用 `evals/skills`，读 `*.md` 的
   frontmatter + 正文组装成 `Skill`），
   合并规则与现有 `skills` / `skills_mode` 一致。不加时行为不变。
2. `_child.py`：`artifact_final` 带上产物**正文全文**（现在只有 title + chars），
   给下面的纯代码断言提供数据源。
3. `checks.py` 新增：
   - `artifact_no_placeholder` —— 扫"待补充 / TODO / 此处省略 / 略 / 占位"等词（交付完整性）
   - `artifact_looks_like_html` —— 网页类产物要以 `<!DOCTYPE html>` 或 `<html` 开头、含 `</html>`
4. `judge.py` 新增两个 rubric：
   - `dependency_bridged` —— 后序委派有没有把前序产物带过去（ID 或要点）
   - `artifact_derives_from` —— 后序产物内容是否真的基于前序产物
5. `dump_artifacts`：HTML 产物按内容嗅探存成 `.html`（人工审计时能直接双击打开）

**顺带修的一个坑（多轮 + 自带技能才触发）**：`graph._feed_waiting` 里是**函数内**
`from proto.skills import SKILLS`，而 `install()` 只换了 `proto.agent.SKILLS` ——
`skills_mode: replace` 的 case 一旦走到"用户答复 → 投给待答任务"，投递会按**内置技能**重跑
（`lesson-prep` / `guided-learning` 内置版是 150 字小技能），或找不到而跳过
（`interactive-webpage` 内置没有）。现有自带技能的 case 都是单轮，所以一直没暴露。
两处一起换已修。

## 五、跑法与预算

```bash
# 先通链路（1 遍，省钱）
uv run python -m evals.run --tag testskills-probe --only t1-chain --reps 1
# 再出通过率
uv run python -m evals.run --tag testskills --only t1-chain,t2-same-turn --reps 3
```

- 单 run 预估 3–10 分钟（长产物 + 多轮 react + 判定），t1×3 + t2×3 并发 4 ≈ 15–30 分钟
- 产物原文落 `evals/runs/<tag>/artifacts/<case>__r<rep>/`，**人工审计是"完整性"的唯一真值来源**
  （判定会被 4000 字预算截断）：HTML 能不能打开、教案符不符合内联模板、导学单与教案是不是同一课

## 六、判定口径

- 硬断言全绿 = 该 run 通过（沿用 harness 口径）
- 有效性断言（`artifact_writes_multithreaded`）不绿 → 同 case 其他断言的通过**不算数**
- 软断言（评分）失败判不通过
- 人工审计产物原文，逐条记"完整性 / 一致性"

## 七、已知风险与观察点

1. 🔴 **跨技能依赖可能根本不成立（本轮最大未知）**：后序技能的执行器是**隔离上下文**，
   它拿到教案只有两条路 —— ① 主 agent 在 `task` 文本里带上产物 ID / 要点，
   ② 执行器自己 `read_artifact`。而主 agent 的系统提示里写着"产物正文不要搬进回复"，
   可能连 `task` 里都不敢带 → 导学单退化成"通用导学单"。**这是最值得看的发现点。**
2. **长指令的副作用**：lesson-prep 内联后约 5k 字指令，判定提示词全量带入
   （`JUDGE_ITEM_BUDGET` 只管产物，不管 instructions），观察判定是否变差、成本涨多少。
3. **并发写竞态**：`store_artifact` 用 `len(ARTIFACTS)+1`（e13 已知），这轮用真实长产物复测。
4. **提问链路**：适配后技能仍会问（课型 / 学情 / 用途），跨轮由 `prepare` 投递 ——
   观察"问了几轮才齐"，以及 t1 的第 2、3 轮原话会不会被误投给已完成的 task（不应发生）。
5. **判定误伤长产物**（F1 已修）—— 这轮是回归验证。

## 八、执行步骤

1. ~~复制 + 适配三个技能~~ ✅ 已完成（`evals/skills/`，逐字副本按用户要求已删，
   来源指纹留在其 README）
2. ~~改 `_child.py` / `checks.py` / `judge.py`~~ ✅ 已完成（见 §四；另修了一个多轮投递的坑，
   见下）
3. 写 case：`t2-same-turn.yaml` ✅（用户指定的测试消息）；`t1-chain.yaml` 待写
4. `--reps 1` 探路 → 看链路通不通、依赖桥没桥上 → 再 `--reps 3` 出通过率
5. 出报告 + 人工审计产物
6. 发现记入 `docs/discussion-log.md`（F9+）；场景进 `docs/scenarios.md`（两边都改）
7. **发现机制缺陷先记档、不擅自改 proto** —— 逐条过完再定改不改

## 九、待确认

1. **适配口径**：按 §二 的机械适配（推荐）；还是另跑一条"原样投喂"看容忍度（t4）
2. **场景范围**：t1 + t2 先跑（推荐）；t3 / t4 要不要
3. **"交互式教案"指哪个技能**：`interactive-webpage`（本批测试技能，纯单文件网页）；
   线上另有 `k12-interactive-courseware`（入参里就有「已有教案」，更贴"教案 → 课件"的语义），
   要的话换它、或两个都跑
