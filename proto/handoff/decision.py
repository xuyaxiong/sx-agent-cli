"""**决策**（一次判断 → 记录）：判断的**两端** ＋ 平台的两句描述。

## 这个文件是什么

一次判断发生时，把它**给它的东西 / 问它的东西 / 它吐出来的 / 平台据此怎么处置**记下来，
渲染成【前提】【问题】【结果】【下一步】四格。**两种判断共用它**：

**这个概念的**三件事都在这儿：**发射**（`emit_decision` —— 校验 `result_by` 后发事件）、
**段名与作者**（`RESULT_BY`）、**渲染**（`render_decision`）。

| 谁在判 | 是什么 | 它是"交接"吗 |
|---|---|---|
| **LLM**（`agent._ask_main` 的试答） | 平台给它的东西 → 它吐出来的东西 | ✅ 是（两端各自都是） |
| **平台**（`executor._verdict` 的「任务完成没」） | 三件结构事实 → 三态 ＋ **依据** | ❌ 不是（**没有 LLM 参与** —— 判据见下） |

## 设计原则（不变量）

1. **它为什么是交接**（用户定的判据）：*"**所谓交接，就是外部给 LLM 什么东西、LLM 吐出
   什么东西** —— 关键在于 **LLM 参与了**。就是因为 LLM 是个**软决策**，所以交接才如此之
   关键，**其回复也很关键**。"* ⇒ **两端各自都是交接**（〔沿革 1〕）。
2. **四格，各有各的类** —— 段名要回答"**这段段体是谁的话**"：

   | 格 | 是什么 | 谁的话 |
   |---|---|---|
   | **前提**（`given`） | 平台**给了它什么** | 平台 |
   | **问题**（`q`） | 平台**问它什么** | 平台 |
   | **结果**（`result`） | **它吐出来的** ← **交接的另一端** | **它** |
   | **下一步**（`next`） | 平台**据此怎么处置** | 平台 |

   ⇒ 按来源那条规矩（**是别人的话必须标；平台自己的框架话不标**），**只有【结果】要标**
   —— 它是 LLM 说的；另外三格是平台的框架话，**不标**。
3. **两种作者不共用一个段名**（`RESULT_BY`）：【结果】= **它吐出来的**（要标来源）；
   【平台判定】= **平台自己的结论**（不标，自证没意义）—— 共用必有一边是**假话**。
4. **渲染只有一处**：记录层和终端都走 `render_decision()`（→ `handoff.render_hop`）；
   **载体各自处理**（记录层加 `[{ts}] ` 前缀、终端逐行上色），**排版归一处**。〔沿革 3〕
5. **不认识的 `result_by` 当场炸** —— 不静默退回 `llm`（那会把平台的结论标成"它说的"）。

## 关键取舍

**只有一套形状。** 四格对两种判断都成立 —— 另起一套 = 同一个问题两副面孔。
平台那一路（「任务完成没」）**没有 LLM 参与**、本来不算交接，但**记录照样留着**：
用户要审计的正是它：

> *"对于完成没，**给它的上下文是啥、结果又是啥、产生结果依据又是啥** ——
>  这些都要能看到才能审计，不然我咋知道怎么调优。"*

## 沿革（改过什么、为什么改）

> 编号是**引用锚**（正文里写 `〔沿革 N〕`）—— **只追加、不改号**，新条目加在**末尾**。

1. **它为什么是交接 —— 我原来的归类是错的**（2026-11-05，用户定的判据）。我原来把这一块
   归成"记录层，不是交接" ✗ —— **错在把"请求 ＋ 结果"合在一起看**，于是整块不像"喂给
   LLM 的东西"，就给它贴了"记录"的标签。**拆开看，两端各自都是交接**（用户原话：
   *"你分开，是不是就不纠结了"*）。
2. **两种判断共用它**（2026-11-05 补）。平台那一路原来只有一次 **LLM 调用**（判定），
   退休了 —— 但**那个判断还在**（改由平台按结构定），所以**记录也跟着留着**。
3. **渲染收敛成一处**（2026-11-05）。在这之前**两个渲染器各排一遍**，而且**排版还不一样**：
   `trace._recorder`（写日志文件）**4 格** / `app/chat.py`（终端上屏）**0 / 2 格** ——
   同一件事两处定义，形状必然漂，正是"改一句文案、下游静默失效"那一族。
   现在两边都走 `render_decision()`。
4. **发射器从总线搬过来**（2026-11-09）。它原来是 `trace.decision()` —— 于是**总线**
   认识 `RESULT_BY`（领域词表），而**记录器又依赖本模块的 `render_decision`**，
   两下一叠，`proto/trace.py` 被顶到了 `handoff/*` **上面**：
   `llm → trace → handoff.decision`，基础能力传递依赖领域词表，`handoff` 也抽不出去。
   ⇒ 按"**总线认识形状，不认识词表**"切开：发射器归本模块（走 `trace.emit`），
   记录器归 `proto/recorder.py`。详见 `trace.py` 沿革 33。
"""

from __future__ import annotations

from typing import Any

from proto import trace
from proto.handoff import bullet_lines, render_hop

#: 事件字段 → **段名 key**（段名本身在 `SECTION` 里，**一处定义**）。
#:
#: ⚠️ `next` **和回执共用同一个段名**【下一步】—— 同一个语义只有一处定义（一套，别分叉）。
#: ⚠️ 顺序就是渲染顺序：前提 → 问题 → 结果 → 下一步（**读的人理解的顺序**）。
#: ⚠️ `result` 那一格**按作者换段名**（见 `RESULT_BY`）—— 下面这张表给的是默认（LLM）。
_DECISION_KEYS: dict[str, str] = {
    "given": "dec_given",
    "q": "dec_q",
    "result": "dec_result",
    "next": "next",
}
#: ⚠️ **顺序不在这里** —— 它在 `HOPS[hop]`（`render_hop` 照着走）。
#: 这张表只管**事件字段 → 段名 key** 的映射。

#: 【结果】那一格**谁写的** → **哪个 hop、哪个段名**。
#:
#: 🔴 段名要回答"**这段段体是谁的话**"（`SOURCE_NOTE` 那条规矩）—— 两种作者
#: **不能共用**一个段名，否则必有一边是**假话**：
#:
#:   - `llm`（试答）：它是**它吐出来的** ← 交接的另一端 → 【结果】**要标**来源
#:   - `platform`（「任务完成没」按结构定）：**平台自己的结论** → 【平台判定】**不标**
#:     （自证没意义，同【平台故障】）
#:
#: ⚠️ **另三格（前提 / 问题 / 下一步）两处共用** —— 都是平台的话，同一个语义一处定义。
RESULT_BY: dict[str, tuple[str, str]] = {
    "llm": ("decision", "dec_result"),
    "platform": ("verdict", "verdict_result"),
}


def emit_decision(
    who: str,
    *,
    q: str = "",
    given: Any = (),
    result: Any = (),
    nxt: str = "",
    result_by: str = "llm",
) -> None:
    """记一次**判断**：前提 → 问题 → 结果 → 下一步（段名与顺序见本模块上面）。

    🔴 **两种判断共用这一条记录形状**（一套，别分叉）—— 它们问的是同一件事
    （**谁基于什么、得出了什么、接下来干什么**），只是"填它的人"不同：

      - **LLM 决策**（`agent._ask_main` 的试答）—— 它是**交接**（两端都是）；
      - **平台的结构判定**（`executor._verdict` 的「任务完成没」）—— **不是交接**
        （没有 LLM 参与 —— 用户判据："关键在于 LLM 参与了"），但四格一样成立：
        前提 = 平台看得见的结构事实、结果 = 三态 ＋ **依据**。〔`trace.py` 沿革 25〕

    🔴 发的是**结构化事件**（字段原样在 `ev` 里），渲染成什么样是**记录器与展示层各自的事**
    —— 展示层不再从渲染文本里反解析。

    `given` / `result` 传**列表**（逐项一行）→ 渲染成 `- ` 列表；传字符串 → 一行。
    ⚠️ 列表化不是装饰：判定那段原来直接把 `j.missing` 打出来，人看到的是
    **Python repr** `['尚未收到…', '教案文本文件产物为空…']` —— 括号引号全是噪音。

    `result_by`：**【结果】那一格是谁写的** —— `"llm"`（试答，要标来源）／
    `"platform"`（「任务完成没」按结构定，平台自己的结论，不标）。它决定那格用哪个段名，
    见 `RESULT_BY`（段名要回答"这是谁的话"，借错了就是假话）。

    ⚠️ **发射器住在这儿、不住总线**：`result_by` 的合法值是**领域词汇**（`RESULT_BY`），
    而总线**只认识形状、不认识词表** —— 所以由本模块发（走 `trace.emit`）。〔沿革 4〕
    """
    if result_by not in RESULT_BY:
        # 打错一个字母就**静默**把平台的结论标成"它说的" —— 宁可当场炸（同 role / kind 那两条）。
        raise ValueError(
            f"decision 的 result_by={result_by!r} 不在 {sorted(RESULT_BY)} 里（见 handoff.decision.RESULT_BY）"
        )
    trace.emit({"type": "decision", "kind": "", "who": who, "q": q,
                "given": given, "result": result, "next": nxt,
                "result_by": result_by, "ts": trace.stamp()})


def render_decision(ev: dict) -> str:
    """决策**事件** → 渲染出来的文本。**记录层和终端共用这一处**。

    ⚠️ 列表 → `- ` 列表项（续行对齐到文字，`bullet_lines`）；字符串 → 一段。
    ⚠️ **空的不渲染**（`render_hop` 的规矩：有内容才出那一段）。
    ⚠️ `result_by` 决定第三个格子用哪个段名 / 哪个 hop（见 `RESULT_BY`）——
    不认识的取值**当场炸**（不静默退回 `llm`：那会把平台的结论标成"它说的"）。
    """
    by = str(ev.get("result_by") or "llm")
    if by not in RESULT_BY:
        raise ValueError(f"未知的 result_by={by!r}：合法值 {sorted(RESULT_BY)}（见 handoff.decision）")
    hop, result_key = RESULT_BY[by]
    keys = {**_DECISION_KEYS, "result": result_key}
    sections: dict[str, str] = {}
    for field, key in keys.items():
        val: Any = ev.get(field)
        items = [str(x) for x in val] if isinstance(val, (list, tuple)) else [str(val or "")]
        items = [x for x in items if x.strip()]
        if not items:
            continue
        if len(items) == 1 and not isinstance(val, (list, tuple)):
            sections[key] = items[0]
        else:
            lines: list[str] = []
            for it in items:
                lines += bullet_lines(it, "")
            sections[key] = "\n".join(lines)
    return render_hop(hop, sections)
