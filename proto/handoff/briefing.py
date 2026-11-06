"""**委托单**（平台 → 执行器）：Task 的结构 ＋ 渲染模板。

    proto/handoff/
      __init__.py    ← 共享词汇表（段名 / 来源标注 / 排版，所有交接点共用那一份）
      briefing.py    ← 本文件：委托单（平台 → 执行器）
      receipt.py     ← 回执（执行器 → 主 agent）
      ask.py · nudge.py · ledger.py · decision.py  ← 另外四跳（schema ＋ 模板同址）

## 这个文件是什么

平台**每一次**把工作交给执行器时，交给它的**那份完整交接**（不是增量）：Task / Turn /
QA 三个 schema ＋ 渲染入口 `render_briefing`，外加执行结果落回 Task 的
`absorb_outcome` / `settle` 与"哑待答"的显式取代 `moot_older_waiting`。
执行器**用完即弃**（每次投递都是一次全新的 `run_task`）—— 所以"这件事到目前为止的
全部已知"必须由平台**重新交给它**（〔沿革 1〕）。

## 设计原则（不变量）

1. **段序 ＝ 读的人理解这件事的顺序**：

       我要做什么 → 用户原话 → 我做过什么 / 问过什么 → 我产出了什么 → 接着干什么

   ⚠️ **一条时间序**：不按来源分段、不"最新在前" —— 后者要让读的人在两段之间来回跳，
   才能把一件事的经过拼起来（〔沿革 5〕）。
2. **来源标注是硬要求**：执行器必须能分清"**哪句是用户说的、哪句是模型说的**"。
   平台**不阻止**主 agent 补充信息（那是它的活），但**必须让下游知道这是谁的话** ——
   否则模型补的东西会被当成用户的事实（〔沿革 2〕）。
3. **Task 的每个字段要么渲染在这里、要么在 `evals/unit_briefing.py` 的排除名单里**
   —— 那里有断言，以后加字段忘了渲染会红。
4. **两处硬机制，都是确定性的（不靠措辞）**：
   ① **排版** —— 段名 0 列 / 段体 2 列 / 每层 +2（`proto.handoff.BODY` ＋ `body_lines`）；
   ② **去重** —— 逐字比较两个**我们自己写的字段**（`qa[-1].q` vs `report`），
      不是从文本里反结构化（成立与否是"恒成立"而不是"碰巧"，所以能断言）。

## 关键取舍

**① 一问一答成对**：`report` / `asked` 都是**覆盖写**，只存答案的话，第二次重入之后
旧答案就成了**没有指涉的孤儿** —— 所以「问」由平台在**收下答案的那一刻**从 `Task.asked`
（`ask_user` 的参数原话）原样截下，不靠任何 LLM 转述（〔沿革 3〕）。

**② 只折叠逐字相同的最后一条，且必须标出来**：读的人分不出"它没说"和"被折叠了" ——
悄悄不贴和悄悄切一刀一样糟；折叠的**字数**另报一行日志（同 `trace.cut` 的做法，〔沿革 12〕）。

**③ 缺的东西显式写出来，不许静默跳过**：有 ID 而取不到是异常（写"产物库里没有这一条"），
`asked` 为空也一样明说 —— **有就说有，没有就说没有**（〔沿革 9〕〔沿革 16〕）。

**④ 说不清就不动**：哑待答的取代只在**两条结构事实**都成立时才做；fixture / 手写 state
一律不碰 —— fail-safe（〔沿革 19〕）。

**⑤ 措辞写死"以新的为准"**：读交接书的是**执行器**（每次重入、用完即弃）——
【问答经过】按时间序排，块尾那句「如前后答复冲突，**以后面的为准**」（`render_briefing`）
就是防它把旧答复当成当前的。〔沿革 17〕

## 遗留问题

- `Task` **没有运行时校验**（`total=False` 是如实）：靠 `delegate` 建全 ＋ 断言兜底 ——
  见 `Task` 的 docstring。

## 沿革（改过什么、为什么改）

> 编号是**引用锚**（正文里写 `〔沿革 N〕`）—— **只追加、不改号**，新条目加在**末尾**。

1. **为什么要这份委托单**（2026-10-29，F15）。执行器**用完即弃**，每次投递都是一次全新的
   `run_task`，所以"这件事到目前为止的全部已知"必须由平台**重新交给它**。原来三条路径
   （首跑 / 试答后重跑 / 答复投递）各拼各的，**用户历次答复被漏掉** —— 用户一次答一个时，
   答第二个就把第一个冲掉了，于是同一个问题问两遍。
2. **来源标注**（2026-11-02）。执行器必须能分清"**哪句是用户说的、哪句是模型说的**" ——
   平台**不阻止**主 agent 补充信息（那是它的活），但**必须让下游知道这是谁的话**。
   实测（b3-smoke2）：执行器把主 agent 编的"三年级/四年级"说成了"**您写的是**三年级/四年级"
   —— 而用户从来没写过。
3. **一问一答成对**（2026-11-02）。`report` 是**覆盖**写的（`absorb_outcome` 无条件覆盖），
   所以只存答案的话，第二次重入之后旧答案就成了**没有指涉的孤儿** —— 那是修过的
   "答第二个把第一个冲掉"的**镜像版**（那次修的是答案，这次是问题）。
4. **短应答必须带上它在回答什么**（2026-11-02）。光一个「A」，执行器既解读不了、
   也核对不了主 agent 的转述 —— 而 `Turn.after` 这一段存在的全部理由就是**对冲转述失真**
   （见 `agent._user_turns` 的 docstring）。
5. **段序改成一条时间序**（2026-11-03）。原来是**按来源分段、且最新在前**：`report`
   （它最新那句）排在 `qa`（更早的历次问）**前面**，读的人得在两段之间来回跳，才能把
   一件事的经过拼起来 —— 实测（`教案日志.txt` R07）读起来是**倒的**。
6. **不再"点名"**（2026-11-03，用户定）。原来由主 agent 用 `based_on_turns=[1,3]` 点哪几轮
   给全文 —— 它错在**时点**："哪几轮相关"是**读任务的时候**才成立的关系（同一条原话，
   对"出导学单"无关、对"改这份教案"就是核心），让**写任务的人**预先声明，等于让他替读的人
   做阅读理解。接口上它也只能靠编号表达，而编号只活在平台内部 —— 实测就数错了（4 轮报成
   5 轮）。**正解不是让模型点得更准，是把"挑"这个动作删掉**：平台只保证两件确定的事 ——
   ① 原话**一字不改**地搬进索引；② **每一轮都在索引里**。至于哪几轮要读全文，由**读的人**
   按需取。**准确不来自"猜得准"，来自"没有猜测环节"。** 同一次起，索引那一层不再报
   `trace.cut`（原来报的是"点名轮之外的被切了几轮" —— 索引行不是"被切掉的正文"）。
7. **`settle` 收敛状态迁移**（2026-11-03，审核实测到的真 bug）。原来散在 7 处手写
   `rec.update(stage="completed", artifacts=outcome.artifacts, …)` —— **无条件覆盖**，
   把"这一轮没产出就不抹掉上一轮产物"的保护**当场抹掉**：待答重入后这一轮没产出，
   Task 上原来记的产物 ID 就没了（台账 / 终端 / 断言看到的都是"这个任务没交付过东西"）。
   同一形状 `check_every_task_has_artifact` 的 docstring 里记过一次（failed 分支），
   这是它在 completed 分支上的翻版。
8. **顶层 import**（2026-11-04 复审 N3）。`artifacts` / `trace` 原来都在**函数内**，纯习惯 ——
   无环（`artifacts` 只依赖 langchain 的工具装饰器；`trace` 只依赖 `config`），提到顶层。
9. **产物取不到要显式写出来**（2026-11-04 复审 R6）。原来 `_index` 用 `if (a := …)`
   把取不到的 ID **悄悄从清单里删掉**，而**同一件事**在回执那边写的是
   「**产物库里没有这一条**」—— 同一份坏数据，执行器在交接书里看到的是"少了一行"、
   在主 agent 那边看到的是"这行有问题"：两副面孔，而且前者是**静默降级**
   （**有 ID 而取不到**是异常，必须说）。
10. **`run_by` 为什么要记**（2026-11-04 复审 E2）。台账截断时要说清"全文在哪"，而这两条路
    的答案**不一样**：`delegate` 那一趟的全文在**回执**里（主 agent 自己的工具返回，就在
    它上下文里）；投递重跑那一趟**没有回执**（平台直接调的 `run_task`），全文压根不在
    主 agent 上下文里。原来台账对两条路都写"全文见那次的回执"—— 投递那条路指过去，
    主 agent 会翻到**上一轮**的回执（内容还是旧的），把旧的当成当前的：
    **这比"找不到"更坏**。
11. **「问」改取 `asked`，不再取 `report`**（2026-11-04）。在这之前「问」取的是 `report`
    （`absorb_outcome` 之前它还没被覆盖，所以"最后一问 == report"当时**恒成立**）。后果是
    交接书里的「问」是**过场**而不是问题（实测 `logs/proto-20261104-152230.log`：「问 1」
    写的是"好的，第一个问题已发出，等你的答复"），而**同一句问题**在透传那条路上走的是
    `Task.asked` —— **一个事实两副面孔**。现在两处都取 `asked`。
12. **逐字相同才折叠，且折叠要留痕**（2026-11-04）。改取 `asked` 之后，"逐字相同"只在
    执行器**把问题原样又说了一遍**时偶然成立 —— 那时两边都渲染就是同一段话贴两遍
    （实测 R07：275 字，占交接书 11%）。**只折叠逐字相同的最后一条，且必须标出来** ——
    悄悄不贴和悄悄切一刀一样糟：读的人分不出"它没说"和"被折叠了"。
13. **不许静默丢没被回答的问题**（2026-11-04，F7）。`_supersede_old` 只在**重派**时取代
    `failed` 的任务；**待答**的问题一律不碰 —— 它必须留着，等用户真的看见它。
14. **`asked` 落进 `Task`**（2026-11-05，`solution.md` §0.65 Y1 的第一步）。在这之前，
    这句原话**只活在当轮**（`ExecOutcome.asked`），任务一进"待答"就丢了 ✗ —— 跨轮只剩
    `report`（执行器最后说的**那段话**，混着过场）。后果：**透传给用户的是那段散文**，
    不是这句干净的原话；而且**没有对象就做不了弹框**（"谁在问、问什么"得是结构化的）。
    ⚠️ 这跟 `fault` 是同一个病、隔一个字段：**结构性事实没落进 Task，下游只能用散文**。
15. **`fault` 落进 `Task`**（2026-11-05，深度审计 P1-1 / A3）。在这之前**故障只活在
    `ExecOutcome` 上**，落到 `Task` 时就只剩 `stage="failed"` 了 —— 于是**同一件事两副
    面孔**：回执说"⚠️ 平台故障，原样再派一次"，下一轮台账说"❌ 它没交付、也没说要什么"
    （那句话的意思是"**执行器**没干成"）。而主 agent 做下一轮决定时看的**是台账**。
    现在**事实只有这一处定义**，回执的【平台故障】与台账的标记都是它的视图；故障的
    "类"（`fault_kind`）同一次落进来，台账据此选引导句。
16. **`asked_text` 抽出来**（2026-11-05，用户定）。这条规则（"取哪句、取不到怎么办"）
    原来**实现了三遍**，而且**只有一遍是对的**：`graph.render_relay`（对话消息）显式说清 ✅；
    `evals/chat.py` 的提示符（输入通道）**静默退回 `report`** ❌；`proto/__main__.py` ×2
    同上 ❌。⇒ 同一次待答、同一个缺失，**两个视图显示不同** —— 而 CLI 那处**更像"正常"**
    （把那段过场当成了问题），**没有任何东西告诉你这是数据缺失**。那正是 `CLAUDE.md` 禁的
    "兼容兜底"：**把违反藏起来**。
17. **判定退休，整条链摘掉**（2026-11-05）。`Task.progress` / `Task.missing`（**判定**吐的
    "已做到 / 还缺"）**没有任何东西再写它们**，两个字段整条链一起摘掉；交接书里那段
    【上次判定】也整段删掉。执行器"上次说到哪"仍然在【问答经过】里（**它自己的原话** ✓
    按时间序 ✓ 那个来源是真的）。
    ⚠️ 同一次，"措辞写死'这是上一轮的情况'"那条规矩举的例子也没了（那段话随【上次判定】
    一起删）—— 现在读交接书的**只有执行器**，同类保护是块尾那句「以后面的为准」。
18. **`settle` 落账也记一行**（2026-11-05）。三态是"**这一趟**跑成什么样"，`Task.stage`
    才是"**这个任务**现在算什么" —— 后者原来只活在 state 里（终端那行 `📋 TASKS` 是拿
    快照现渲染的），trace 里没有"tac2700f3 → completed"这一行，复盘时接不上（用户要审计
    的"完成没"里，这就是**任务那一侧的结果**）。
19. **哑待答显式取代**（2026-11-06，实测踩到）。用户对卡片的问题**没走 `↩` 答复通道**，
    而是又发了一条新消息（提示符时序，见 `evals/chat._read_card_answer` 的 docstring）——
    主 agent 照常对同技能**重新委派**，新任务完成、交付了。而旧待答 `_supersede_old`
    **不碰**（〔沿革 13〕）⇒ 它**永远挂着**：每轮结束卡片都重新摆一遍，而"再答它"会拿
    **旧任务**重跑执行器（60–120s）。实测会话：`logs/proto-20261105-174508.log` 的
    `t8b06800d`（新任务 `tb742ef69` 已交付，旧问题还在问"你想要什么主题"）。
"""

from __future__ import annotations

from typing import Any, TypedDict

# ⚠️ 顶层 import（这两个原来都在**函数内**，纯习惯）：无环 ——
# `artifacts` 只依赖 langchain 的工具装饰器，`trace` 只依赖 `config`。〔沿革 8〕
from proto import artifacts as artifacts_mod
from proto import trace
from proto.handoff import (
    BODY,
    FaultKind,
    OutcomeStatus,
    TaskStage,
    body_lines,
    bullet_lines,
    section_head,
)

# ──────────────────────────── schema：委托单上有什么 ────────────────────────────


class Turn(TypedDict):
    """**一轮用户原话**（平台硬搬的快照，不经任何转述）。

    🔴 `after` 是**紧邻的前一条 AI 消息**（纯位置关系，平台搬，不判断语义）——
    用户答「A」时，脱离它回答的那句（选项列表在里面）就没意义。〔沿革 4〕
    """

    n: int
    text: str
    after: str


class QA(TypedDict):
    """**一问一答成对** —— 免得「答」脱离了「问」。

    `report` / `asked` 都是**覆盖**写的（`absorb_outcome`），所以只存答案的话，
    第二次重入之后旧答案就成了没有指涉的孤儿。问题由**平台**在收下答案的那一刻
    从 `Task.asked`（`ask_user` 的参数原话）原样截下，不靠任何 LLM 转述。〔沿革 3〕

    🔴 **`q` 是 `asked`，不是 `report`**：`report` 是执行器最后说的**那段话**，
    混着过场 —— 拿它当「问」等于没有指涉（〔沿革 11〕）。⚠️ `asked` 取不到
    （fixture / 手写 state）时存**空串**，渲染那边会明说"这一条没留下问题原话"；
    **不许**拿 `report` 顶替（那正是这次修的错）。
    """

    by: str  # "用户" | "主 agent"
    q: str  # 执行器问的原话（`ask_user` 的参数 —— 取不到时是空串，不是 `report`）
    a: str  # 答复


class Task(TypedDict, total=False):
    """**委托单**：一次委派出去的工作的全部已知。

    ⚠️ `total=False` 是**如实**：`case.initial_tasks` 注入的 Task 可能只有部分字段
    （见 `evals/_child.py`）。**没有运行时校验** —— 靠 `delegate` 建全 + 断言兜底。
    """

    id: str
    skill: str
    task: str  # 主 agent 写的"这次要什么"（**它说的，可能加了料**）
    stage: TaskStage  # 五态，词汇表在 `handoff/__init__`
    artifacts: list[str]  # **产出**的产物 ID
    refs: list[str]  # **输入引用**的产物 ID（两个方向，别混）
    report: str  # 执行器最后一轮的原话（**覆盖写**）
    #: 🔴 **它申报要的那条信息** —— `ask_user(question)` 参数里的**原话**（**覆盖写**）。
    #: 结构性事实必须落进 Task，下游才有对象可用（不然只能用散文）—— 与 `fault` 同一个病。
    #: 见〔沿革 14〕。
    asked: str
    #: 🔴 **平台故障的原因**（非空 = 这一轮**根本没跑成**，**不是执行器干不了**；空 = 没故障）。
    #: 事实只有这一处定义：回执的【平台故障】与台账的标记都是它的视图。〔沿革 15〕
    fault: str
    #: 🔴 **平台故障的"类"**（`FaultKind`：`transient` / `rejected` / `timeout` / `""`）—— 台账据此选
    #: 引导句（"原样再委派" vs "换措辞/改内容"）。**结构，不许从 `fault` 文本反解析**。
    #: ⚠️ 与 `fault` 同生共死：每次 `settle` 由 `absorb_outcome` **覆盖写**（正常跑完清空）。
    fault_kind: FaultKind
    qa: list[QA]
    user_turns: list[Turn]  # 派发那一刻的快照
    agent_tried: bool  # 平台强制试答过没有
    #: 这个 Task **最近一次运行是谁起的** —— `"delegate"`（主 agent 派发的）
    #: / `"feed"`（用户答复后平台投递重跑的）。
    #:
    #: 🔴 台账截断时要说清"全文在哪"，而两条路的答案**不一样**（投递那条路
    #: **没有回执**）—— 指错会让主 agent 翻到**上一轮**的回执、把旧的当成当前的。
    #: 〔沿革 10〕
    run_by: str  # "delegate" | "feed"


# ──────────────────────────── 渲染：注入前那一瞬才拼 ────────────────────────────


#: **索引行**里每轮露出的开头字数（`_one_line` 的默认 limit）。
#:
#: ⚠️ 它不是"切到多少"，是"**一行有多长**" —— 见 `turn_lines` 的分层说明。
_one_line_limit = 40


def asked_text(t: dict) -> str:
    """待答任务 → **用户该答的那一句**（`Task.asked`）。取不到就**显式说清**。

    🔴 **只负责"取哪句"，不负责排版** —— 两个视图**载体不同**（一个是对话里的
    一条消息、一个是输入通道的标签），**该各自排版**（"视图可以有很多个，
    事实源只能有一个"）。所以这里返回的是**内容**，不带 `- 「skill」问：` 那种框架。

    ⚠️ 返回**单行**（`_flat` 压平）：它要能直接用在 `input()` 的提示符里。
    压平只动空白、不动内容（同 `_flat` 的注释）。

    ⚠️ `asked` 为空**只有两个来源**（产品路径上不该发生）：`case.initial_tasks`
    注入的 fixture、手写的 state。

    这条规则（"取哪句、取不到怎么办"）原来**实现了三遍、只有一遍是对的** ——
    最坏的那遍**静默退回 `report`**（那正是 `CLAUDE.md` 禁的"兼容兜底"）。〔沿革 16〕
    """
    q = _flat(t.get("asked"))
    if q:
        return q
    # ⚠️ **不许静默退回 `report`**（那正是 F18 要修的错）：说清"没有"，再把它
    #    最后说的那段附上 —— **有就说有，没有就说没有**。
    return (f"（**这一条没留下问题原话**；它最后说的是："
            f"{_flat(t.get('report')) or '（它一个字都没说）'}）")


def _flat(text: object) -> str:
    """`_one_line` **压成一行**之后的样子 —— 压平与比长度必须用**同一个口径**。

    否则换行多的短消息会被误判成"超长"（`_one_line` 先压平再判长度）。
    """
    return " ".join(str(text or "").split())


def _one_line(text: str, limit: int = _one_line_limit) -> str:
    """压成一行 + 截到 limit 字 —— 索引里**一行一轮**，不能让它撑成多行。

    🔴 **这不是"截断"，是"索引"**（见 `turn_lines`）：短到一行装得下的，
    这一行**就是全文**；装不下的，全文走 `read_user_turn(轮次)` 按需取。
    所以这里既不报 `trace.cut`，也不需要"切了"的标记 —— `（X 字）` 和 `…`
    已经把"这轮多大、你只看到开头"说清楚了（审计的四种截断状态里，
    这是 📋「清单 + 按需」，不是 ⚠️「显式截断」）。
    """
    flat = _flat(text)
    return flat if len(flat) <= limit else flat[:limit] + "…"


def turn_lines(turns: list[dict]) -> list[str]:
    """**用户原话索引** —— 一行一轮：序号 · 字数 · 开头 · 它在回答什么。

    ## 分层（渐进式披露）

    | 层 | 内容 | 成本 |
    |---|---|---|
    | **常驻（本函数）** | 每轮**一行** | **∝ 轮次数**，与文本量**无关** |
    | **按需** | `read_user_turn(n)` 取那一轮全文 ＋ 它回答的那句全文 | 读的人自己触发 |

    🔴 **关键性质：短到一行装得下的轮次，这一行就是全文**（实测 226 份会话里
    **91% 的用户消息 ≤40 字**、中位 5 字）—— 所以"索引"和"全文"在绝大多数情况下
    是同一个东西；真正的长正文一律走按需。**用户粘 5 万字，这一层也只涨一行。**

    🔴 **平台不"点名"**：不猜"哪几轮相关"，只保证 ① 原话**一字不改**地搬进索引、
    ② **每一轮都在索引里**；哪几轮要读全文，由**读的人**按需取。〔沿革 6〕
    """
    out: list[str] = []
    for t in turns:
        n, text = t.get("n"), str(t.get("text") or "")
        out.append(f"{BODY}- 第 {n} 轮（{len(text)} 字）：{_one_line(text)}")
        after = str(t.get("after") or "").strip()
        if after:
            # 🔴 **短应答必须带上它在回答什么**：光一个「A」，执行器既解读不了、
            # 也核对不了主 agent 的转述 —— 而这一段存在的全部理由就是**对冲转述失真**。
            # 见 `agent._user_turns` 的 docstring。〔沿革 4〕
            out.append(f"{BODY}  ↳ **它在回答的上一句**：「{_one_line(after)}」")
    return out


def render_briefing(task: dict) -> str:
    """把 Task 渲染成**执行器的输入** —— 所有重入路径的唯一入口。

    ⚠️ 名字是 task 不是 given：它是**整份交接**（任务 / 上次做到哪 / 历次答复 /
    已产出产物 / 可读产物），不是"这一轮的增量"—— 执行器用完即弃，每次都是重入。

    ⚠️ **渲染出来的就是全部**：没有"给人看的另一份"（用户：*"你显示给我看的，
    就是传给 llm 看的"*）—— 日志里 `📥 INJECT` dump 的是这个函数的返回值本身，
    不是它的改写。
    """

    blocks: list[str] = []

    # ── ① 任务：这次要什么 ──────────────────────────────────────────────
    # 🔴 **来源标注**：执行器必须能分清"**哪句是用户说的、哪句是模型说的**"。
    # 平台**不阻止**主 agent 补充信息（那是它的活），但**必须让下游知道这是谁的话** ——
    # 否则模型补的东西会被当成用户的事实。〔沿革 2〕
    blocks.append("\n".join([*section_head("task"), *body_lines(task.get("task", ""), BODY)]))

    # ── ② 用户原话（平台硬搬，未经转述）—— 渐进式披露：清单常驻、全文按需 ──
    turns = task.get("user_turns") or []
    if turns:
        # 索引的**形状**在 `turn_lines`（分层 ＋ 平台不"点名"那条规矩都在那里）；
        # 这里只负责加段名、来源标注和块尾那句逃生口。
        block = section_head("user_turns") + turn_lines(turns)
        block.append(
            f"{BODY}（**这是事实来源**；【任务】是这次的要求，两者冲突时**以原话为准**。"
            "要看哪一轮的**完整上下文**（它回答的上一句 ＋ 原话）就用 `read_user_turn(轮次)` 读，"
            "不要凭那几十个字猜。）"
        )
        blocks.append("\n".join(block))
        # ⚠️ 这里**不报 `trace.cut`**：索引行**不是"被切掉的正文"**，是这一层**本来
        #    就只放一行** —— 审计的四种截断状态里它属于 📋「清单 + 按需」（正文按需取），
        #    不是 ⚠️「显式截断」。"这轮多大、你只看到开头"由 `（X 字）` ＋ `…` ＋
        #    块尾那句逃生口说清楚。〔沿革 6〕

    # ── ③ 问答经过：**一条时间序**（历次问→答，末尾接上"它最后说的那句"）──
    #
    # 🔴 **一问一答成对**：`report` 是**覆盖**写的（`absorb_outcome` 无条件覆盖），
    # 所以只存答案的话，第二次重入之后旧答案就成了**没有指涉的孤儿** —— 那正是修过的
    # "答第二个把第一个冲掉"的**镜像版**（那次修的是答案，这次是问题）。问题由**平台**
    # 在收下答案的那一刻从 `Task.asked`（`ask_user` 的参数原话）原样截下，不靠任何
    # LLM 转述。〔沿革 3〕
    qa = [x for x in (task.get("qa") or [])
          if isinstance(x, dict) and str(x.get("a") or "").strip()]
    report = (task.get("report") or "").strip()
    # 🔴 **确定性去重**（判据：见模块 docstring ②）—— 逐字比较两个**我们自己写的字段**，
    # 不是从文本里反结构化。成立与否是"恒成立"而不是"碰巧"，所以能断言。〔沿革 12〕
    folded = bool(qa) and bool(report) and str(qa[-1].get("q") or "").strip() == report
    if qa or report:
        block = section_head("answered")
        for i, x in enumerate(qa, 1):
            q = str(x.get("q") or "").strip()
            by = str(x.get("by") or "用户")
            if q:
                # `q` 是 `ask_user` 的**参数原话**（结构 ✓，不是从散文里抽的）——
                # 所以这里**没有**"抽取哪句才是问题"那种语义判断要做（见 §0.7）。
                block.append(f"{BODY}- **问 {i}**（执行器原话）：")
                block += body_lines(q, BODY * 2)
            else:
                # ⚠️ 空 = **这一条没记下 `ask_user` 的参数**（fixture / 手写 state），
                #    不等于"它没说话" —— 它说的那段话在下面「它最后说的那句」里。
                #    说清是哪种，别让读的人以为它一个字都没说（同 `finalize._one`）。
                block.append(f"{BODY}- **问 {i}**：（**这一条没留下问题原话**）")
            # ⚠️ 「答」是**和「问」平级的一项**，不是「问」的续行 ——
            # 原来是 `  **用户** 答：`（和问的续行同一个缩进），答看起来像问的一部分。
            block.append(f"{BODY}- **答 {i}**（**{by}**）：")
            block += body_lines(str(x["a"]), BODY * 2)
        if folded:
            block.append(
                f"{BODY}（**它最后说的那句**就是上面「问 {len(qa)}」，逐字相同 —— "
                "原样，**不重复贴**。）"
            )
        elif report:
            # `report` 不是任何一问。**两条路都会走到这里**：
            #   - 首跑 / 试答后重跑：还没有人答过，它那段话是**唯一的进展**；
            #   - 有 `qa` 的那些轮：「问」取的是 `asked`，与 `report` 本就不是同一句 ——
            #     那段话是它自己的收尾，不该丢。**一个字都不能少**，所以照原样给它一块。
            # ⚠️ "这一轮还没有人答"只在**没有 `qa`** 时才是真话（〔沿革 11〕）。
            tail = "" if qa else "，这一轮还没有人答"
            block.append(f"{BODY}- **它最后说的那句**（原样{tail}）：")
            block += body_lines(report, BODY * 2)
        if len(qa) > 1:
            block.append(f"{BODY}（如前后答复冲突，**以后面的为准**）")
        blocks.append("\n".join(block))
        if folded:
            # 折叠**也要报一刀**（同 `trace.cut`）：标记在**文本里**（读的人看得见），
            # 这一行在**日志里**（复盘的人看得见）—— 两者都要有。

            trace.note(
                f"🔁 折叠重复 · 交接书：它最后说的那句 == 上面「问 {len(qa)}」"
                f"（{len(report)} 字，原样不重复贴）"
            )

    def _index(aids: list[str]) -> list[str]:
        """产物 ID 列表 → 清单行（**含缺失**）。

        🔴 **缺失要显式写出来，不许静默跳过**：有 ID 而取不到是**异常**，必须说 ——
        同一份坏数据在回执那边写的是「**产物库里没有这一条**」，两处不许两副面孔。
        〔沿革 9〕
        """
        out: list[str] = []
        for aid in aids:
            a = artifacts_mod.get(aid)
            if not a:
                out.append(f"- {aid}（**产物库里没有这一条**）")
                continue
            out.append(
                f"- {aid}《{a['title']}》（{len(a['content'])} 字）"
                f"—— {a.get('summary') or '（无梗概）'}"
            )
        return out

    produced = _index([a for a in (task.get("artifacts") or []) if a])
    if produced:
        blocks.append("\n".join([
            *section_head('already_produced'),
            *[f"{BODY}{x}" for x in produced],
            f"{BODY}（这次要**在它基础上补全**，提交完整版；不要从零重写。）",
        ]))

    refs = _index([a for a in (task.get("refs") or []) if a])
    if refs:
        blocks.append("\n".join([
            *section_head('readable'),
            *[f"{BODY}{x}" for x in refs],
            f"{BODY}（这是清单，不是正文。要用哪一份就用 read_artifact 读全文，不要凭标题猜。）",
        ]))

    if report or qa:
        # 只有**重入**才说这句：首跑说"接着干"会让它以为有前情
        blocks.append("\n".join([
            *section_head("resume"),
            f"{BODY}接着已有的工作继续，**不要重复已完成的工作**。",
        ]))
    return "\n\n".join(blocks)


def absorb_outcome(rec: dict, outcome: Any) -> None:
    """把执行器的结果**写进 Task** —— 四个字段一个都不能少（F15 的同类：存了才可能喂）。

    ⚠️ `artifacts` 只在非空时覆盖：执行器可能这一轮没产出，不能把上一轮的抹掉。
    """
    rec["report"] = outcome.report
    # 它申报要的那条信息（`ask_user` 的原话）—— 与 `report` 一样**覆盖写**：
    # 这一轮没申报就清空（它已经答上了 / 已经交付了，"待答"这个状态就结束了）。
    rec["asked"] = outcome.asked
    # 平台故障**也落进 Task**（理由见 `Task.fault`）—— 与 `report` 一样**覆盖写**：
    # 正常跑完就清空，不留上一次的故障（不然一个瞬时的故障会在台账里阴魂不散）。
    rec["fault"] = outcome.fault
    # 故障的"类"跟着一起落（台账据此选引导句，见 `Task.fault_kind`）—— 同样覆盖写。
    rec["fault_kind"] = outcome.fault_kind
    if outcome.artifacts:
        rec["artifacts"] = list(outcome.artifacts)


#: 执行器三态 → `Task.stage` —— **只有这一处**映射（`ExecOutcome.status` 是封闭枚举，
#: 所以这里是穷举的：`completed` / `waiting_for_input` / **其余一律 failed**）。
#: 键 = `OutcomeStatus`、值 = `TaskStage` —— **两边都是词汇表里的类型**，
#: 打错一个键名 mypy 会报；"键是不是刚好覆盖三态"由 `unit_handoff` 的守卫盯着（N4）。
_STATUS_TO_STAGE: dict[OutcomeStatus, TaskStage] = {
    "completed": "completed",
    "waiting_for_input": "waiting_for_input",
    "incomplete": "failed",
}


def settle(rec: dict, outcome: Any) -> str:
    """**把一次执行结果落进 Task** —— 状态迁移的唯一入口（原来散在 7 处手写，〔沿革 7〕）。

    两件事一起做完，顺序不能反：
      ① `absorb_outcome`：report / artifacts
         （**artifacts 只在非空时覆盖** —— 这一轮没产出，不能把上一轮交付的抹掉）；
      ② `stage`：由 `outcome.status` 映射（见 `_STATUS_TO_STAGE`）。

    ⚠️ 调用方常常已经先 `absorb_outcome` 过一次（重入前要 `render_briefing`）——
    这里再写一遍是**幂等**的，换来的是"不可能漏写"。
    """
    absorb_outcome(rec, outcome)
    rec["stage"] = _STATUS_TO_STAGE[outcome.status]
    # 🔴 **落账也记一行**：三态是"**这一趟**跑成什么样"，`Task.stage` 才是"**这个任务**
    #    现在算什么" —— 后者原来只活在 state 里（终端那行 `📋 TASKS` 是拿快照现渲染的），
    #    trace 里没有"tac2700f3 → completed"这一行，复盘时接不上（用户要审计的"完成没"里，
    #    这就是**任务那一侧的结果**）。
    #    ⚠️ **不改取值**（`_STATUS_TO_STAGE` 仍是唯一映射）—— 这里只是把它**记出来**。
    #    ⚠️ **无 kind = 纯记录、不上屏**：终端那行快照已经在说同一件事（`chat._turn_summary`），
    #       再上屏就是同一个事实两副面孔。〔沿革 18〕
    trace.note(
        f"Task 落账 · {rec.get('id')}「{rec.get('skill')}」："
        f"status={outcome.status} → stage={rec['stage']}"
        + ("｜**平台故障**：这一轮没跑成" if outcome.fault else "")
    )
    return rec["stage"]


def moot_older_waiting(done: dict, tasks: list[dict]) -> list[dict]:
    """**刚完成的这条**让哪些更早的待答成了哑问题 → 显式标 `superseded`（**不是静默丢**）。

    ## 判据（两条，都是**结构事实**，不是猜）

      ① **新任务已完成**（`done` 刚 settle 成 completed）—— 活已经干完了；
      ② **用户在这条之后又说过话** —— `user_turns` 快照**前缀延长**
         （`len(t) < len(done)` 且 `done` 的快照以 `t` 的为前缀）。

    快照**没延长**（同一轮里重派）或**没有快照**（fixture / 手写 state）→ **一律不动**：
    那正是 F7 的场景（主 agent 自己编了答案重派，用户从没被问过）—— 问题必须留着，
    等用户真的看见它。**说不清就不动**（同 `ledger._SRC_DEFAULT` 的 fail-safe，〔沿革 13〕）。

    ⚠️ **和 `_supersede_old` 的分工**：那条管"重派取代 failed"（在**建**的时候）；
    这条管"完成了、且用户已另起话题"（在**落账**的时候）—— 两条都不碰
    "用户还没机会说话"的待答。

    ⚠️ 返回的是**改过 stage 的副本**（原 dict 不动，同 `_supersede_old`）——
    交给 reducer 按 id 合并。取代**留痕**（`trace.note`，kind=`supersede`）。

    为什么要它：用户没走 `↩` 答复通道、又发了新消息时，旧待答**永远挂着**
    （每轮重摆、答它＝重跑旧任务）—— 见〔沿革 19〕。
    """
    done_turns = done.get("user_turns") or []
    out: list[dict] = []
    for t in tasks:
        if t.get("id") == done.get("id") or t.get("skill") != done.get("skill"):
            continue
        if t.get("stage") != "waiting_for_input":
            continue
        t_turns = t.get("user_turns") or []
        # 判据②：快照必须**前缀延长**（用户在这条之后又说过话）。
        if (not t_turns or len(t_turns) >= len(done_turns)
                or done_turns[: len(t_turns)] != t_turns):
            continue
        out.append({**t, "stage": "superseded"})
        # 🔴 **取代要留痕**（"显式"那一半）：说清谁被谁取代、依据是什么 ——
        #    判据可审计（"判断必须可审计"：依据"没有"也要显式写）。
        trace.note(
            f"🔁 {t['id']}（{t['skill']}）的待答**已被新任务 {done['id']} 取代** —— "
            f"依据：用户在这条之后又说过话（原话快照 {len(t_turns)} → {len(done_turns)} 轮），"
            f"且新任务已完成；旧问题不再摆出（**显式取代，不是静默丢**）",
            kind="supersede",
        )
    return out
