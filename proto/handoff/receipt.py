"""**回执**（执行器 → 主 agent）：Receipt 的结构 ＋ 渲染模板 —— `briefing.py` 的**镜像**。

> 🔴 **为什么要有它**（用户 2026-11-02）：
>
> "交付内容跟委托任务交付的东西一样，**得形式化**呀，不要那么随意懂吗。
>  而且**不要截断** —— 我很关心 subagent 给主 agent 交付了什么呀，你截断了我怎么看。"
>
> 原来七个返回口各拼各的字符串，还带 `report[:150]` / `[:300]` / `[:60]` 的截断 ——
> 于是"执行器到底交代了什么"，在**主 agent 的上下文里**是**残的**：
> 它拿到的是一段被切掉尾巴的话，而那段话正是它写回复、也是人读日志要看的**交付内容**。

| | 交接书（`briefing.py`） | 回执（本文件） |
|---|---|---|
| 方向 | 平台 → 执行器 | 执行器 → 主 agent |
| 形状 | **固定段名 · 每段标来源 · 不截断** | 同左 |

## 🔴 对象与文本是**同一份东西的两种形态**

```
ExecOutcome ──build_receipt()──▶ Receipt（结构化）
                                  ├─ render_receipt(r) ──▶ 文本 ──▶ ToolMessage.content（给模型看）
                                  └─ 原样挂 additional_kwargs["handoff"]（搭同一趟车）
                                       └─▶ 记录/审计层**读它**，不从文本反推
```

**用户 2026-11-03 定的硬规矩**：*"禁止你反结构化任何 llm 正文输出 ——
结构化处理的前提一定是 llm 输出本身就是结构化的。"*
所以记录层**没有**、也不许有"把回执文本解析回段"的代码 —— 那正是 2026-11-02
那次事故的根（手抄的标记全部失配、12560 字被静默切成 1200）。
"""

from __future__ import annotations

from typing import Any, TypedDict

from proto.handoff import (
    DELIVERY,
    HOPS,
    SECTION,
    SECTION_LAYOUT,
    SOURCE_NOTE,
)

# ──────────────────────────── schema：回执上有什么 ────────────────────────────


class Receipt(TypedDict, total=False):
    """**回执**：一次委派的结果，平台要交给主 agent 的全部内容。

    🔴 它是**结构化**的 —— 渲染成文本只是为了喂给模型（`render_receipt`），
    **记录/审计层读的是这个对象**，绝不从渲染出来的散文里解析。

    ⚠️ `sections` 的键就是 `SECTION` 的键，值的顺序由 `HOPS` 决定 ——
    这样"渲染出来的段"和"记录层显示的段"**结构上必然一致**。
    """

    kind: str  # 出口分类：DELIVERY 的键 + rejected_skill / rejected_turns / rejected_artifact
    sections: dict[str, str]  # 段名 key → 该段的**内容原文**（不含段名、不含来源标注）


# ──────────────────────────── 构造：结果 → 对象 ────────────────────────────


def build_receipt(outcome: Any, *, agent_answer: str = "", note: str = "") -> Receipt:
    """执行器的结果 ＋ 处置提示 → **回执对象**。**注入上下文之前**，一切都在对象上。

    ⚠️ 各段是**有条件**的（有产物才出【产出的产物】、试答过才出【你补的信息】）——
    条件写在这里，`HOPS['receipt']` 声明的 7 段是**上限**。
    """
    from proto.artifacts import ARTIFACTS

    if outcome.status == "completed":
        kind = "completed" if outcome.artifacts else "completed_in_reply"
    elif outcome.status == "waiting_for_input":
        kind = "waiting"
    else:
        kind = "failed"

    sections: dict[str, str] = {}

    # ① 交付结果：一行说清成没成、交付了几样
    if kind == "completed":
        sections["deliver"] = DELIVERY["completed"].format(len(outcome.artifacts))
    elif kind == "completed_in_reply":
        sections["deliver"] = DELIVERY["completed_in_reply"]
    elif kind == "waiting":
        sections["deliver"] = DELIVERY["waiting"]
    else:
        sections["deliver"] = DELIVERY["failed"].format(outcome.rounds)

    # ② 产出的产物：逐条列，**带真实路径**（主 agent 要答"文件在哪"）
    produced = [a for a in (outcome.artifacts or []) if a]
    if produced:
        block: list[str] = []
        for aid in produced:
            a = ARTIFACTS.get(aid)
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
            "（正文在**产物库**里，你的上下文里没有 —— 要看就用 `read_artifact('art_001')` 读，"
            "不要凭标题猜。回复里报 ID ＋ 路径就够了。）"
        )
        sections["produced"] = "\n".join(block)

    # ③ 执行器说：**全文，一个字都不截**（这是"它交付了什么"）
    #
    # 🔴 **已知膨胀点（判据③：没截断的要有标记，便于以后优化）**：
    # 执行器把成果**写在回复正文里**时（"正文交付"，`solution.md` §0.7），
    # 这段就是**整份交付物** —— 它会原样进主 agent 的上下文，**隔离在这一条路上失效**。
    # 实测（2026-11-03，`x-handoff-skillfile`）：主上下文 13598 字，其中回执 **12560 字（92%）**，
    # 而那份产物 15679 字。**不截断是用户明确要求的**（"你截断了我怎么看"），所以这不是 bug，
    # 是代价 —— 要优化的话，方向是**给正文交付一条硬透传**（像待答那样平台直接贴给用户），
    # 而不是切回截断。见 `docs/handoff-audit.md` §9.4 新 5。
    report = str(outcome.report or "").strip()
    if report:
        sections["report"] = report

    # ④ 判定给的进度 / 缺失项：**没完成才给**
    if outcome.status != "completed":
        if outcome.progress:
            sections["progress"] = str(outcome.progress)
        missing = [x for x in (outcome.missing or []) if x]
        if missing:
            sections["missing"] = "\n".join(f"- {x}" for x in missing)

    # ⑤ 主 agent 自己试答补的信息（成对记进了 `qa`，这里回显一下）
    if agent_answer.strip():
        sections["agent_answer"] = agent_answer.strip()

    # ⑥ 下一步：处置提示（各出口不同）
    if note:
        sections["next"] = note

    return {"kind": kind, "sections": sections}


def build_rejection(kind: str, detail: str, available: str) -> Receipt:
    """**派发被拒**（不建 Task）→ 回执对象。

    `kind` ∈ `rejected_skill` / `rejected_turns` / `rejected_artifact`；
    `available` 是平台列出来的"现在有什么"（能力 / 轮次 / 产物）。
    """
    return {
        "kind": kind,
        "sections": {"not_dispatched": detail, available_key(kind): available},
    }


#: 拒绝口 → 它列出的"现在有什么"那一段的段名 key
_REJECTED_AVAILABLE: dict[str, str] = {
    "rejected_skill": "available_skills",
    "rejected_turns": "available_turns",
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
    keys = HOPS["receipt"] if receipt.get("kind") not in _REJECTED_AVAILABLE else HOPS["receipt_rejected"]
    lines: list[str] = []
    sections = receipt.get("sections") or {}
    for key in keys:
        text = sections.get(key)
        if not text:
            continue
        head = SECTION[key] + SOURCE_NOTE.get(key, "")
        lines.append(f"{head}{text}" if SECTION_LAYOUT.get(key) == "inline" else f"{head}\n{text}")
    return "\n".join(lines)
