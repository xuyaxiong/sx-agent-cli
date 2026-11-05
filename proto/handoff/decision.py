"""**决策**（LLM 调用 → 记录）：一次 LLM 调用的**两端** ＋ 平台的两句描述。

## 🔴 它为什么是交接（用户 2026-11-05 定的判据）

> *"**所谓交接，就是外部给 LLM 什么东西、LLM 吐出什么东西** —— 关键在于 **LLM 参与了**。
> 就是因为 LLM 是个**软决策**，所以交接才如此之关键，**其回复也很关键**。"*

我原来把这一块归成"记录层，不是交接" ✗ —— **错在把"请求 ＋ 结果"合在一起看**，
于是整块不像"喂给 LLM 的东西"，我就给它贴了"记录"的标签。
**拆开看，两端各自都是交接**（用户原话：*"你分开，是不是就不纠结了"*）。

## 四格，各有各的类

| 格 | 是什么 | 谁的话 |
|---|---|---|
| **前提**（`given`） | 平台**给了它什么** | 平台 |
| **问题**（`q`） | 平台**问它什么** | 平台 |
| **结果**（`result`） | **它吐出来的** ← **交接的另一端** | **它** |
| **下一步**（`next`） | 平台**据此怎么处置** | 平台 |

⇒ 按来源那条规矩（**是别人的话必须标；平台自己的框架话不标**），
**只有【结果】要标** —— 它是 LLM 说的。另外三格是平台的框架话，**不标**。

## 🔴 渲染只有一处（2026-11-05 收敛）

在这之前**两个渲染器各排一遍**，而且**排版还不一样**：

| 渲染器 | 缩进 |
|---|---|
| `trace._recorder`（写日志文件） | **4 格** |
| `evals/chat.py`（终端上屏） | **0 / 2 格** |

⇒ 同一件事两处定义，形状必然漂 —— 正是"改一句文案、下游静默失效"那一族。
现在两边都走 `render_decision()`（→ `handoff.render_hop`），
**载体各自处理**（记录层加 `[{ts}] ` 前缀、终端逐行上色），**排版归一处**。
"""

from __future__ import annotations

from typing import Any

from proto.handoff import bullet_lines, render_hop

#: 事件字段 → **段名 key**（段名本身在 `SECTION` 里，**一处定义**）。
#:
#: ⚠️ `next` **和回执共用同一个段名**【下一步】—— 同一个语义只有一处定义（一套，别分叉）。
#: ⚠️ 顺序就是渲染顺序：前提 → 问题 → 结果 → 下一步（**读的人理解的顺序**）。
_DECISION_KEYS: dict[str, str] = {
    "given": "dec_given",
    "q": "dec_q",
    "result": "dec_result",
    "next": "next",
}
#: ⚠️ **顺序不在这里** —— 它在 `HOPS["decision"]`（`render_hop` 照着走）。
#: 这张表只管**事件字段 → 段名 key** 的映射。


def render_decision(ev: dict) -> str:
    """决策**事件** → 渲染出来的文本。**记录层和终端共用这一处**。

    ⚠️ 列表 → `- ` 列表项（续行对齐到文字，`bullet_lines`）；字符串 → 一段。
    ⚠️ **空的不渲染**（`render_hop` 的规矩：有内容才出那一段）。
    """
    sections: dict[str, str] = {}
    for field, key in _DECISION_KEYS.items():
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
    return render_hop("decision", sections)
