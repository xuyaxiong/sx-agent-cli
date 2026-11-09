"""**回执**（执行器 → 主 agent）：Receipt 的结构 ＋ 渲染模板 —— `briefing.py` 的**镜像**。

## 这个文件是什么

执行器一趟活干完（或没干完），平台把结果**结构化**成 `Receipt`（`build_receipt` /
`build_rejection`），**注入上下文那一刻**才渲染成文本（`render_receipt` → `handoff.render_hop`）；
同一个对象**搭同一趟车**进记录 / 审计层（`additional_kwargs["handoff"]`）——
对象与文本是**同一份东西的两种形态**（见「设计原则 1」）。

| | 交接书（`briefing.py`） | 回执（本文件） |
|---|---|---|
| 方向 | 平台 → 执行器 | 执行器 → 主 agent |
| 形状 | **固定段名 · 每段标来源 · 不截断** | 同左 |

## 设计原则（不变量）

1. **对象与文本是同一份东西的两种形态** —— 文本只是给模型看的一个视图，事实源是对象（〔沿革 1〕）：

   ```
   ExecOutcome ──build_receipt()──▶ Receipt（结构化）
                                     ├─ render_receipt(r) ──▶ 文本 ──▶ ToolMessage.content（给模型看）
                                     └─ 原样挂 additional_kwargs["handoff"]（搭同一趟车）
                                          └─▶ 记录/审计层**读它**，不从文本反推
   ```

2. **不许反解析**（用户定的）：*"禁止你反结构化任何 llm 正文输出 ——
   结构化处理的前提一定是 llm 输出本身就是结构化的。"*
   记录层**没有**、也不许有"把回执文本解析回段"的代码（〔沿革 2〕）。
3. **交付内容一个字都不截**（用户定的）：*"交付内容跟委托任务交付的东西一样，**得形式化**呀，
   不要那么随意懂吗。而且**不要截断** —— 我很关心 subagent 给主 agent 交付了什么呀，
   你截断了我怎么看。"* 代价与优化方向见「关键取舍 ②」。
4. **段名 / 来源标注 / 顺序全从词汇表取**（`SECTION` / `SOURCE_NOTE` / `HOPS` / `BODY`，
   渲染走 `render_hop`）—— 本文件**一个字面量都不写**，两边不可能失配。

## 关键取舍

**① 【下一步】的文案住在这里，不写在调用点**（〔沿革 9〕）。`docs/handoff-samples.md`
是**生成的**（`experiments/render_handoffs.py`），它要渲染**同一份**文案 ——
两处各写一遍，样例必然陈旧；一处定义，两边都从这儿取。

**② 【执行器说】会很长，但那是代价、不是 bug**（〔沿革 4〕）。不截断是用户明确要求的，
所以这里长只有两个正当来源：① 执行器话多（含礼貌收尾、过程叙述）；
② **任务本来就不需要文件产物**（纯分析），成果本来就在它的话里。
**要优化就优化这两条** —— ⚠️ **不许为"正文交付"造任何机制**（那是反模式）。

## 遗留问题

- **【执行器说】的已知膨胀点**（判据③：没截断的要有标记，便于以后优化）——
  实测最坏：主上下文 13598 字，其中回执 **12560 字（92%）**（〔沿革 4〕）。

## 沿革（改过什么、为什么改）

> 编号是**引用锚**（正文里写 `〔沿革 N〕`）—— **只追加、不改号**，新条目加在**末尾**。

1. **为什么要有回执**（2026-11-02，用户定的）。原来七个返回口各拼各的字符串，还带
   150 / 300 / 60 字三档截断（都在 `report` 上）—— 于是"执行器到底交代了什么"，
   在**主 agent 的上下文里**是**残的**：它拿到的是一段被切掉尾巴的话，而那段话正是
   它写回复、也是人读日志要看的**交付内容**（用户原话见「设计原则 3」）。
2. **"不许反解析"是那次事故逼出来的**（2026-11-02 事故 → 2026-11-03 定成硬规矩）。
   记录层原来靠**手抄的分段标记**把回执文本切回段 —— 回执一改格式，标记**全部失配**，
   **12560 字被静默切成 1200**。用户当天定下硬规矩（原话见「设计原则 2」），
   从此事实源只有对象（`additional_kwargs["handoff"]`）。
3. **~~`rejected_turns`~~ 删**（2026-11-03）。轮次不再由主 agent 点名（见
   `briefing.turn_lines` 的分层说明）—— "点了不存在的轮次"这件事**结构上不可能**了。
4. **【执行器说】为什么长、为什么不能截**（2026-11-03 实测 `x-handoff-skillfile`）。
   主上下文 13598 字，其中回执 **12560 字（92%）** —— 不截断是用户明确要求的
   （*"你截断了我怎么看"*），所以这不是 bug，是**代价**。⚠️ **不许拿"正文交付"解释
   这一段**（用户当天纠正）：那是**反模式** —— 平台根本不保证那条路径，真技能
   （`zhiwo` / `lesson-prep` / `guided-learning` / `interactive-webpage`）**都已经改成
   `write_artifact` 落产物**。所以这里长只有两个正当来源：① 执行器话多（含礼貌收尾、
   过程叙述）；② **任务本来就不需要文件产物**（纯分析），成果本来就在它的话里。
5. **段体不许顶在 0 列**（2026-11-03，用户：*"return 那块日志还是很乱"*）。
   段体顶在 0 列时，执行器的原话和平台框架话**左边界一模一样**，读的人分不清哪句是谁说的。
6. **平台故障是独立出口**（2026-11-04 复审 N2/R8）。故障时这一轮**根本没跑成**，
   与"执行器干不了"（failed）是两回事：出口分类、交付行、下一步**全都不同** ——
   所以它自己一个 `kind`，不借 failed 的文案（"推了 N 轮"对故障是假话），
   也不塞进【执行器说】（那句的来源标注是"它说的"，而故障是**平台说的**）。
7. **`asked` 进回执，是日志定位逼出来的**（2026-11-04）。主 agent 的回复**写在
   `finalize` 注入问题之前** —— 实测同一轮的时间线：
   回执 15:23:14.770 → 它回复 15:23:19.193 → 平台注入问题 15:23:19.198。
   也就是说它写"派了什么、还差什么"时**根本不知道问的是什么**，只能编（实测编出
   "比如年级、课时、学情等"，而执行器问的是**课型**）。透传保证**用户**不丢，
   但主 agent 那句话是假的 —— 而它正是要写回复的人。
8. **顶层 import 是复审 N3 的漏网之鱼**（2026-11-04）—— N3 那份名单里**没有**
   `receipt.py`。规则本身（无环、纯习惯、走模块 + 属性）见正文 import 处。
9. **【下一步】的文案搬到本文件**（2026-11-05）。`docs/handoff-samples.md` 是**生成的**
   （`experiments/render_handoffs.py`），它要渲染**同一份**文案 —— 两处各写一遍，
   样例必然陈旧（**实测就陈旧过**：样例写「重派」、代码写「重新委派」）。
10. **判据要说清**（2026-11-05，用户：*"产生结果依据又是啥啊 —— 这些都要能看到才能审计"*）。
    `completed` 那档原来只有一句"够不够"，**从没说"够"的标准是什么** —— 它只能看执行器
    自己说的话点头，判了也无据可依、更无从调优。标准**本来就有**：技能指令里写着交付格式
    （平台系统提示也是这么告诉它的），这里只是把那条路**指出来**（平台不替它判，
    也不新造一套判据）。
11. **`read_artifact` 那句写死过 `art_001`**（2026-11-04 实测踩到）。实测
    `x-prep-lotus`：这次产出的是 `art_002`（导学单），提示却让人去读 `art_001`
    （那是上一份**教案**）—— **指向"存在但内容不对"的出处，比指不到更坏**（同 E2 那条
    教训）。现在用占位形式（和【下一步】那句**同一套**写法）；断言：`unit_receipt` ③' ——
    拿一个**不是 art_001** 的产物断。
12. **渲染收敛到 `render_hop`**（2026-11-05）。收敛之前本文件是**唯一一处"声明驱动"**
    的渲染（自己写了个循环遍历 `HOPS`）；收敛时清掉了 `BODY` / `HOPS` / `body_lines` /
    `section_head` 的 import（渲染那四样归 `render_hop` 管）—— 本文件只剩
    "挑这一跳是哪一组段"；那条路现在台账 / 试答 / 推回去 / 透传也走。
13. **~~【已做到】/【还缺】~~ 两段删**（2026-11-05）。它们是**判定**给的，判定退休后
    没有来源了 —— "它说过的原话"仍在【执行器说】里 ✓ 那个来源是真的。
14. **故障从一档变三档**（2026-11-06，外测 SX-07 ＋ 外审 F9）。原来只有"抖动"一档
    （"重试过一次仍失败"）；先分出 `rejected`（4xx 是上游对**这份内容**的确定性拒绝，
    原样重发**会再被拒**），再分出 `timeout`（**整趟跑超了墙钟** —— 它**一直在干活**，
    只是这一趟没干完，老超时说明活太大、该拆小）。三档**各有各的处置**，不许借彼此的
    文案 —— "重试过一次仍失败"对后两档都是假话（根本没重试），"原样重发会再被拒"
    对超时也是假话（内容没问题）；分类从结构上取（见 `handoff.FaultKind`），
    **不许从 `fault` 文本反解析**。
15. **故障渲染收成表 ＋ 兜底不再宣称事实**（2026-11-06，第三轮外审 P2-1）。沿革 14
    说的是"不许借彼此的文案"，可**兜底那一支没跟着改**：交付行 / 故障段 / 下一步 /
    `agent` 的 trace note **四处**的 `else` 都还兜到"重试过一次仍失败"。实测：
    给一个词汇表外的档 `fault_kind="quota"`，
    **一张回执里同一句假话出现两次**。⇒ ①三条 `if/elif/else` 收成
    `_DELIVER_BY_FAULT` / `_NEXT_BY_FAULT` / `_FAULT_SECTION` **三张表**（键 =
    `FaultKind`）；②第四处 `NOTE_BY_FAULT` 从 `agent.py` **搬进来** —— 散在业务侧的话
    覆盖守卫够不着；③四处**各一条覆盖断言**（`unit_handoff` ⑬），加档忘了补 → 当场红；
    ④兜底换成 `DELIVERY_FAULT_UNCLASSIFIED` 那套 —— **一个事实都不宣称**。
16. **`wall_s` 的分钟数不许说"0 分钟"**（2026-11-06，第三轮外审 P3-1）。`:.0f` 在
    `<60s` 时四舍五入到 **0** —— "跑到 **0 分钟**还没交"是句读不通的话（实测：
    `wall_s=7.19` / `29.0` 都这么说）。改 `max(1, round(wall_s / 60))`。
    ⚠️ 今天 `fault_kind="timeout"` 的唯一生产者是 `MAX_WALL_S=600` 的墙钟，所以
    **当前不可达**；但把 `httpx.ReadTimeout` 归到 `timeout` 是很自然的下一步，
    那一步一落地它就会露出来。
"""

from __future__ import annotations

from typing import Any, TypedDict

# ⚠️ 顶层 import（无环，纯习惯）—— 走**模块 + 属性**（`artifacts_mod.get(...)`），
#    不走 `from … import get`。〔沿革 8〕
from proto import artifacts as artifacts_mod
# ⚠️ **只 import 它真正用的** —— 渲染那四样（`BODY` / `HOPS` / `body_lines` /
#    `section_head`）归 `render_hop` 管，这里再 import 一份就是"两处定义"的种子。〔沿革 12〕
from proto.handoff import (
    DELIVERY,
    DELIVERY_FAULT_REJECTED,
    DELIVERY_FAULT_TIMEOUT,
    DELIVERY_FAULT_UNCLASSIFIED,
    render_hop,
)

# ──────────────────────────── schema：回执上有什么 ────────────────────────────


class Receipt(TypedDict, total=False):
    """**回执**：一次委派的结果，平台要交给主 agent 的全部内容。

    🔴 它是**结构化**的 —— 渲染成文本只是为了喂给模型（`render_receipt`），
    **记录/审计层读的是这个对象**，绝不从渲染出来的文本里解析。

    ⚠️ `sections` 的键就是 `SECTION` 的键，值的顺序由 `HOPS` 决定 ——
    这样"渲染出来的段"和"记录层显示的段"**结构上必然一致**。
    """

    kind: str  # 出口分类：DELIVERY 的键 + rejected_skill / rejected_artifact
    sections: dict[str, str]  # 段名 key → 该段的**内容原文**（不含段名、不含来源标注）


# ──────────────────────────── 【下一步】：各出口的处置提示 ────────────────────────────

#: 三个**不带变量**的出口的【下一步】文案 —— 见 `next_note`。
_NEXT: dict[str, str] = {
    "waiting": (
        "平台已登记为待答，**并会负责把这个问题转达给用户**，"
        "用户的答复也会由平台投回给它 —— 你**不用**重复问，"
        "更不要自己替用户定一个答案。"
    ),
    "failed": (
        "它推满了轮次**既没交产物、也没说要什么信息**。两种可能，你看回执定：\n"
        "- 它**真的干不了** → 换个说法再 `delegate` 一次，或向用户说明这次没做成；\n"
        "- 它其实**把答案写在了回复里**（上面【执行器说】）→ 平台不认正文交付，"
        "但你可以**原样转达**给用户，或者让它**交付成产物**再来一次。"
    ),
    "fault": (
        "这次是**平台故障**，**不是任务本身的问题** —— 原样再 `delegate` 一次即可"
        "（不用改措辞、不用改参数、不用问用户）。"
    ),
}

#: 🔴 **【交付】那一行的三张脸** —— 一档一条，各说各的处置，**不许借彼此的文案**。
#: ⚠️ `transient` 就是 `DELIVERY["fault"]`（那段话本来就是照着抖动写的）。
#: ⚠️ `timeout` 那份带一个 `{}` = "跑了多久"（说不清时留空，**别编**）。
#: ⚠️ **覆盖由 `unit_handoff` ⑬ 守着**。〔沿革 19〕
_DELIVER_BY_FAULT: dict[str, str] = {
    "transient": DELIVERY["fault"],
    "rejected": DELIVERY_FAULT_REJECTED,
    "timeout": DELIVERY_FAULT_TIMEOUT,
}

#: 🔴 **【下一步】里 `fault` 那档的三张脸** —— 一档一条，各说各的处置。
#: ⚠️ **`transient` 就是 `_NEXT["fault"]`**：那段话本来就是照着抖动写的，不是另起一份
#: （一套，别分叉）。
#: ⚠️ **覆盖由 `unit_handoff` ⑬ 守着**（键 == `FaultKind` 去掉 `""`）：加第五档忘了补
#: 这张表 → 当场红，而不是静默走兜底说假话。〔沿革 19〕
_NEXT_BY_FAULT: dict[str, str] = {
    "transient": _NEXT["fault"],
    "rejected": (
        "这次是**上游拒绝了请求**（4xx）—— **不是任务本身的问题，也不是抖动**："
        "**原样重发同一份内容会再被拒**。\n"
        "**换措辞 / 改内容**再 `delegate` 一次；改不动就向用户说明这一段被拦了。"
    ),
    "timeout": (
        "这次是**时间用完了**（不是它干不了，也不是内容有问题）—— "
        "**可以原样再 `delegate` 一次**。\n"
        "⚠️ 要是**老是**超时，说明这件活对一轮来说太大：**把它拆小**"
        "（分成两次委派）。"
    ),
}

#: 🔴 **词汇表之外的故障档的【下一步】** —— 同 `DELIVERY_FAULT_UNCLASSIFIED` 的规矩：
#: **一个事实都不宣称**（不知道是哪一档，就别替它说"原样再委派"还是"换措辞"）。
#: ⚠️ 用词走体系：**「委派」**，不许单写"派"（守卫 `unit_handoff_form` ⑥ 会红）。
_NEXT_FAULT_UNCLASSIFIED = (
    "**平台侧没跑成**，而且这一档**平台没分类** —— 先看上面那段里的原始原因，"
    "再决定是原样再委派一次还是换措辞。"
)

#: 🔴 **【平台故障】那一段的三张脸** —— `{detail}` 是原始原因（`outcome.fault`）。
#: ⚠️ 头一句**按档不同**：抖动/被拒是"根本没**跑成**"，超时是"根本没**跑完**"
#: （它一直在干活）—— 所以整段进表，不共用一个开头。
#: ⚠️ **覆盖同样由 `unit_handoff` ⑬ 守着**。〔沿革 19〕
_FAULT_SECTION: dict[str, str] = {
    "transient": (
        "这一轮**根本没跑成**：{detail}\n"
        "平台侧的基础设施抖动，**重试过一次仍然失败** —— 不是任务本身的问题，"
        "也不是执行器没干成。\n"
        "**原样再 `delegate` 一次**即可（不用改措辞、不用改参数、不用问用户）。"
    ),
    "rejected": (
        "这一轮**根本没跑成**：{detail}\n"
        "**上游拒绝了这次请求**（4xx）—— 不是基础设施抖动，也不是执行器没干成；"
        "**原样重发同一份内容会再被拒**。\n"
        "**换措辞 / 改内容**再 `delegate` 一次；改不动就向用户说明这一段被拦了。"
    ),
    "timeout": (
        "这一轮**根本没跑完**：{detail}\n"
        "**时间用完了**（整趟跑超了平台的上限）—— 不是它干不了，也不是内容有问题；"
        "它**一直在干活**，只是这一趟没干完。\n"
        "**可以原样再 `delegate` 一次**；要是**老是**超时，说明活太大，拆小一点再委派。"
    ),
}

#: 词汇表外的故障档的【平台故障】段 —— 把原始原因摆出来，**不替它编处置**。
_FAULT_SECTION_UNCLASSIFIED = (
    "这一轮**根本没跑成**：{detail}\n"
    "**平台侧没跑成**，而且这一档**平台没分类** —— 上面那句就是原始原因；"
    "**别按「重试过 / 没重试」去推断**，看它是什么再决定怎么重新委派。"
)

#: 🔴 **`delegate` 那条 trace note 的三张脸**（`agent._cmd` 用）。
#: ⚠️ 它**也住在这里**：同一个词汇表的第四处渲染 —— 散在 `agent.py` 里的话，覆盖守卫
#: 够不着它（第三轮外审 P2-1 点的就是这个：**四处里只有一处有守卫**）。〔沿革 19〕
NOTE_BY_FAULT: dict[str, str] = {
    "transient": "**平台故障**（重试过仍失败）→ 没跑成",
    "rejected": "**上游拒绝了这次请求**（4xx）→ 没跑成（原样重发会再被拒）",
    "timeout": "**时间用完了**（整趟超墙钟）→ 没跑完（原样再委派可以；老超时就拆小）",
}

#: 词汇表外的故障档的 note —— 同规矩，**不宣称事实**。
NOTE_FAULT_UNCLASSIFIED = "**平台侧没跑成**（原因未分类）"


def next_note(kind: str, *, skill: str = "", fault_kind: str = "") -> str:
    """出口 → 回执的【下一步】那段话（`kind` ∈ `Receipt.kind`）。

    🔴 **它住在这里、不写在调用点**：`docs/handoff-samples.md` 是**生成的**，要渲染
    **同一份**文案 —— 一处定义，两边都从这儿取。〔沿革 9〕

    ⚠️ 不认识的 `kind` → **KeyError**（当场炸）—— 别静默给一句错的处置。
    ⚠️ `skill`：只有 `completed` 那档用得上（判据以**技能声明的交付标准**为准，
    所以要能点出技能名 —— 见下面那段注释）。
    ⚠️ `fault_kind`：只有 `fault` 那档用得上 —— **三档故障三种处置**（分类从结构上取，
    见 `handoff.FaultKind`）。〔沿革 14〕
    """
    if kind == "completed":
        # 🔴 **判据要说清**：平台**不替它判**，只把判据**指出来** —— 技能指令里就写着
        #    交付格式（平台系统提示也是这么告诉它的），够不够由它拿这个标准自己看。〔沿革 10〕
        return (
            "**你判**：这次交付够不够？（不够就换个说法重新委派，或向用户说明）\n"
            f"判据以**技能「{skill}」声明的交付标准**为准"
            f"（不确定标准是什么就用 `read_skill('{skill}')` 读原文）——"
            "交付账就在上面【产出的产物】里。\n"
            "要核对内容就用 `read_artifact('<产物 ID>')` 读全文 —— 正文不在你的上下文里。\n"
            "⚠️ 它说的话在上面【执行器说】里，**过一眼** —— 它可能在话里说了什么"
            "（这次它没调 `ask_user`，平台不替它转达）。"
        )
    if kind == "fault":
        # 🔴 **三档故障三种处置**（分类从结构上取，见 `handoff.FaultKind`）——
        #    走表不走 if/elif：加第五档忘了补表，`unit_handoff` ⑬ 当场红，
        #    而不是静默走兜底去说一句假话。〔沿革 14〕〔沿革 19〕
        return _NEXT_BY_FAULT.get(fault_kind, _NEXT_FAULT_UNCLASSIFIED)
    return _NEXT[kind]


# ──────────────────────────── 构造：结果 → 对象 ────────────────────────────


def _kind_of(outcome: Any) -> str:
    """**出口分类** —— 🔴 **平台故障先判**：这一轮**根本没跑成**，与"执行器干不了"
    （failed）是两回事 —— 出口分类、交付行、下一步**全都不同**，所以它自己是一个
    `kind`，不借 failed 的文案（"推了 N 轮"对故障是假话）。〔沿革 6〕
    """
    if outcome.fault:
        return "fault"
    if outcome.status == "completed":
        return "completed"
    if outcome.status == "waiting_for_input":
        return "waiting"
    return "failed"


def _deliver_line(outcome: Any, kind: str) -> str:
    """① **交付行**：一行说清成没成、交付了几样。"""
    if kind == "fault":
        # 🔴 **三档故障三条交付行**：各有各的**处置**，不许借彼此的文案 ——
        #    `rejected` 那档尤其不许说"重试过一次仍失败"（**根本没重试**，见 `trace`）。
        #    ⚠️ 走表不走 if/elif：加档忘了补表 → `unit_handoff` ⑬ 当场红。〔沿革 14〕〔沿革 19〕
        #    ⚠️ 跑了多久**说不清就不说**（fixture / 手写 outcome 没有 `wall_s`）——
        #    留空比编一个数强（同 `Task.asked` 取不到时的规矩）。
        #    ⚠️ `max(1, …)`：**别说出"跑到 0 分钟"**（<60s 时 `:.0f` 会四舍五入到 0，
        #    而"跑了 0 分钟"是句读不通的话）。〔沿革 16〕
        mins = (f"（跑到 {max(1, round(outcome.wall_s / 60))} 分钟还没交）"
                if getattr(outcome, "wall_s", 0) else "")
        template = _DELIVER_BY_FAULT.get(outcome.fault_kind, DELIVERY_FAULT_UNCLASSIFIED)
        # ⚠️ 三张模板**都不带别的占位符**（只有 timeout 那个 `{}`），所以统一 format 是安全的。
        return template.format(mins)
    if kind == "completed":
        return DELIVERY["completed"].format(len(outcome.artifacts))
    if kind == "waiting":
        return DELIVERY["waiting"]
    return DELIVERY["failed"].format(outcome.rounds)


def _produced_block(aids: list[str]) -> str | None:
    """② **产出的产物**：逐条列，**带真实路径**（主 agent 要答"文件在哪"）。

    ⚠️ 缺失的产物**显式写出来**，不静默跳过 —— 与 `briefing._index` 同一副面孔。
    """
    produced = [a for a in aids if a]
    if not produced:
        return None
    block: list[str] = []
    for aid in produced:
        a = artifacts_mod.get(aid)
        if not a:
            block.append(f"- {aid}（**产物库里没有这一条**）")
            continue
        block.append(
            f"- {aid}《{a['title']}》（{len(a['content'])} 字）"
            f"—— {a.get('summary') or '（无梗概）'}"
        )
        if a.get("path"):
            block.append(f"  文件：{a['path']}")
    block.append(
        # ⚠️ **不许写死 ID**：用占位形式，和【下一步】那句**同一套**写法 ——
        #    指到"存在但内容不对"的出处，比指不到更坏（同 E2 那条教训）。〔沿革 11〕
        "（正文在**产物库**里，你的上下文里没有 —— 要看就用 `read_artifact('<产物 ID>')` 读，"
        "不要凭标题猜。回复里报 ID ＋ 路径就够了。）"
    )
    return "\n".join(block)


def build_receipt(outcome: Any, *, agent_answer: str = "", note: str = "") -> Receipt:
    """执行器的结果 ＋ 处置提示 → **回执对象**。**注入上下文之前**，一切都在对象上。

    ⚠️ 各段是**有条件**的（有产物才出【产出的产物】、试答过才出【你补的信息】）——
    条件写在这里，`HOPS['receipt']` 声明的 8 段是**上限**。

    ⚠️ **它自己只做拼装**（2026-11-09 拆）：出口分类 / 交付行 / 产物清单各在一个私有件里。
    """
    kind = _kind_of(outcome)
    sections: dict[str, str] = {"deliver": _deliver_line(outcome, kind)}

    # ①b 它要的信息：**`ask_user` 的参数原话**（结构 ✓ —— 不是从它的话里挑出来的）
    #
    # 🔴 **为什么回执里必须有它**：主 agent 的回复**写在 `finalize` 注入问题之前** ——
    #    不给它这一句，它写"派了什么、还差什么"时**只能编**。〔沿革 7〕
    # ⚠️ **不塞进【交付】**：那是平台的框架话，混进执行器的原话就串了来源标注
    #    （这套代码一直在治"哪句是谁说的"）。也**不是【执行器说】**：那段是它
    #    最后说的**整段话**，混着过场 —— 用户要答的是问题，不是那段独白。
    # ⚠️ `asked` 缺失时（fixture / 手写 state）**不许静默降级**：说清"这一条没留下
    #    问题原话"，再让下面的【执行器说】把那段话摆出来（同 `finalize._one`）。
    if kind == "waiting":
        asked = str(getattr(outcome, "asked", "") or "").strip()
        sections["asked"] = asked or (
            "（**这一条没留下问题原话** —— 下面是它最后说的那段，可能混着过场）"
        )

    # ② 产出的产物
    produced = _produced_block(outcome.artifacts or [])
    if produced:
        sections["produced"] = produced

    # ③ 执行器说：**全文，一个字都不截**（这是"它交付了什么"）
    #
    # 🔴 **已知膨胀点**（判据③：没截断的要有标记，便于以后优化）：它交代得长的时候，
    #    这一段会**整段进主 agent 的上下文** —— 这是**代价**不是 bug，优化方向见
    #    模块头「关键取舍 ②」。〔沿革 4〕
    report = str(outcome.report or "").strip()
    if report:
        sections["report"] = report

    # ⚠️ ~~【已做到】/【还缺】~~ 两段删：它们是**判定**给的，判定退休后没有来源了 ——
    #    "它说过的原话"仍在【执行器说】里 ✓ 那个来源是真的。〔沿革 13〕

    # ④ 主 agent 自己试答补的信息（成对记进了 `qa`，这里回显一下）
    if agent_answer.strip():
        sections["agent_answer"] = agent_answer.strip()

    # ⑤ 下一步：处置提示（各出口不同）
    if note:
        sections["next"] = note

    # ⑥ 平台故障：**说清是谁的锅 + 该怎么办**
    #    ⚠️ 单起一段，不塞进【执行器说】—— 那句话的来源标注是"**它说的**"，
    #    而这段是**平台说的**（这套代码一直在治"来源标注不能串"）。〔沿革 6〕
    if outcome.fault:
        # 🔴 **三档故障三段话**：一档一条，**整段进表**（头一句就不一样：抖动/被拒是
        #    "根本没**跑成**"，超时是"根本没**跑完**"—— 它一直在干活）。
        #    ⚠️ 走表不走 if/elif：加档忘了补表 → `unit_handoff` ⑬ 当场红。
        #    ⚠️ 兜底**一个事实都不宣称**（原来兜到"重试过一次仍然失败"，那对
        #    `rejected`/`timeout` 都是假话）。〔沿革 14〕〔沿革 19〕
        section = _FAULT_SECTION.get(outcome.fault_kind, _FAULT_SECTION_UNCLASSIFIED)
        sections["fault"] = section.format(detail=outcome.fault)

    return {"kind": kind, "sections": sections}


def build_rejection(kind: str, detail: str, available: str) -> Receipt:
    """**派发被拒**（不建 Task）→ 回执对象。

    `kind` ∈ `rejected_skill` / `rejected_artifact`；
    `available` 是平台列出来的"现在有什么"（技能 / 产物）。
    """
    return {
        "kind": kind,
        "sections": {"not_dispatched": detail, available_key(kind): available},
    }


#: 拒绝口 → 它列出的"现在有什么"那一段的段名 key。〔沿革 3〕
_REJECTED_AVAILABLE: dict[str, str] = {
    "rejected_skill": "available_skills",
    "rejected_artifact": "available_artifacts",
}


def available_key(kind: str) -> str:
    """拒绝口 → 它列出的那一段的段名 key（**一处映射**，别在两处各写一份）。"""
    return _REJECTED_AVAILABLE[kind]


# ──────────────────────────── 渲染：注入前那一瞬才拼 ────────────────────────────


def render_receipt(receipt: Receipt) -> str:
    """回执对象 → **给主 agent 看的文本**。段名 / 来源标注 / 排布全部从词汇表取。

    ⚠️ **一个字面量都不写** —— 记录层用的是同一张表，所以两边不可能失配。
    """
    # 🔴 **渲染收敛到 `handoff.render_hop`**（所有交接单共用一条路）——
    #    这里只负责挑"这一跳是哪一组段"（派发被拒那条走 `receipt_rejected`）。〔沿革 12〕
    #    ⚠️ 排版（段名 0 列 → 来源 / 段体 2 列）是**共用规矩**（`handoff.BODY`）——
    #    顶在 0 列时，执行器的原话和平台框架话**左边界一模一样**。〔沿革 5〕
    hop = "receipt_rejected" if receipt.get("kind") in _REJECTED_AVAILABLE else "receipt"
    return render_hop(hop, receipt.get("sections") or {})
