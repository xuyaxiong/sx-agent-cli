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

# ⚠️ 顶层 import（2026-11-04 复审 N3 的**漏网之鱼**：它的名单里没有 receipt.py）——
# 无环，纯习惯。走**模块 + 属性**（`artifacts_mod.get(...)`）而不是 `from … import get`。
from proto import artifacts as artifacts_mod
from proto.handoff import (
    BODY,
    DELIVERY,
    HOPS,
    body_lines,
    section_head,
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


# ──────────────────────────── 构造：结果 → 对象 ────────────────────────────


def build_receipt(outcome: Any, *, agent_answer: str = "", note: str = "") -> Receipt:
    """执行器的结果 ＋ 处置提示 → **回执对象**。**注入上下文之前**，一切都在对象上。

    ⚠️ 各段是**有条件**的（有产物才出【产出的产物】、试答过才出【你补的信息】）——
    条件写在这里，`HOPS['receipt']` 声明的 8 段是**上限**。
    """
    # 🔴 **平台故障**先判（2026-11-04 复审 N2/R8）：这一轮**根本没跑成**，
    #    与"执行器干不了"（failed）是两回事 —— 出口分类、交付行、下一步**全都不同**，
    #    所以它自己是一个 `kind`，不借 failed 的文案（"推了 N 轮"对故障是假话）。
    if outcome.fault:
        kind = "fault"
    elif outcome.status == "completed":
        kind = "completed"
    elif outcome.status == "waiting_for_input":
        kind = "waiting"
    else:
        kind = "failed"

    sections: dict[str, str] = {}

    # ① 交付结果：一行说清成没成、交付了几样
    if kind == "fault":
        sections["deliver"] = DELIVERY["fault"]
    elif kind == "completed":
        sections["deliver"] = DELIVERY["completed"].format(len(outcome.artifacts))
    elif kind == "waiting":
        sections["deliver"] = DELIVERY["waiting"]
    else:
        sections["deliver"] = DELIVERY["failed"].format(outcome.rounds)

    # ①b 它要的信息：**`ask_user` 的参数原话**（结构 ✓ —— 不是从它的话里挑出来的）
    #
    # 🔴 **为什么回执里必须有它**（2026-11-04 日志定位）：主 agent 的回复**写在
    #    `finalize` 注入问题之前**。实测同一轮（`logs/proto-20261104-152230.log`）：
    #    回执 15:23:14.770 → 它回复 15:23:19.193 → 平台注入问题 15:23:19.198。
    #    也就是说它写"派了什么、还差什么"时**根本不知道问的是什么** —— 只能编
    #    （实测编出"比如年级、课时、学情等"，而执行器问的是**课型**）。
    #    透传保证**用户**不丢，但主 agent 那句话是假的，而它正是要写回复的人。
    #
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

    # ② 产出的产物：逐条列，**带真实路径**（主 agent 要答"文件在哪"）
    produced = [a for a in (outcome.artifacts or []) if a]
    if produced:
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
            # ⚠️ **不许写死 ID**（2026-11-05 实测踩到）：这里原来写的是 `read_artifact('art_001')`
            #    —— 实测 `x-prep-lotus`：这次产出的是 `art_002`（导学单），提示却让人去读
            #    `art_001`（那是上一份**教案**）。**指向"存在但内容不对"的出处，比指不到更坏**
            #    （同 E2 那条教训）。用占位形式，和【下一步】那句**同一套**写法。
            #    断言：`unit_receipt` ⑥ —— 拿一个**不是 art_001** 的产物断。
            "（正文在**产物库**里，你的上下文里没有 —— 要看就用 `read_artifact('<产物 ID>')` 读，"
            "不要凭标题猜。回复里报 ID ＋ 路径就够了。）"
        )
        sections["produced"] = "\n".join(block)

    # ③ 执行器说：**全文，一个字都不截**（这是"它交付了什么"）
    #
    # 🔴 **已知膨胀点（判据③：没截断的要有标记，便于以后优化）**：
    # 执行器的交代长的时候，这一段会**整段进主 agent 的上下文**。
    # 实测（2026-11-03，`x-handoff-skillfile`）：主上下文 13598 字，其中回执 **12560 字（92%）**。
    # **不截断是用户明确要求的**（"你截断了我怎么看"），所以这不是 bug，是代价。
    #
    # ⚠️ **别拿"正文交付"来解释这一段**（2026-11-03 用户纠正）：那是**反模式** ——
    # 平台根本不保证那条路径，真技能（`zhiwo` / `lesson-prep` / `guided-learning` /
    # `interactive-webpage`）**都已经改成 `write_artifact` 落产物**。
    # 所以这里长，只有两个正当来源：① 执行器话多（含礼貌收尾、过程叙述）；
    # ② **任务本来就不需要文件产物**（纯分析），成果本来就在它的话里。
    # **要优化就优化这两条**，不要为"正文交付"造机制。
    report = str(outcome.report or "").strip()
    if report:
        sections["report"] = report

    # ⚠️ 原来这里出【已做到】/【还缺】两段（**判定**给的）—— 判定退休后没有来源了，
    #    2026-11-05 一起删。"它说过的原话"仍在【执行器说】里 ✓ 那个来源是真的。

    # ⑤ 主 agent 自己试答补的信息（成对记进了 `qa`，这里回显一下）
    if agent_answer.strip():
        sections["agent_answer"] = agent_answer.strip()

    # ⑥ 下一步：处置提示（各出口不同）
    if note:
        sections["next"] = note

    # ⑦ 平台故障：**说清是谁的锅 + 该怎么办**（2026-11-04 复审 N2/R8）
    #    ⚠️ 单起一段，不塞进【执行器说】—— 那句话的来源标注是"**它说的**"，
    #    而这段是**平台说的**（这套代码一直在治"来源标注不能串"）。
    if outcome.fault:
        sections["fault"] = (
            f"这一轮**根本没跑成**：{outcome.fault}\n"
            "平台侧的基础设施抖动，**重试过一次仍然失败** —— 不是任务本身的问题，"
            "也不是执行器没干成。\n"
            "**原样再 `delegate` 一次**即可（不用改措辞、不用改参数、不用问用户）。"
        )

    return {"kind": kind, "sections": sections}


def build_rejection(kind: str, detail: str, available: str) -> Receipt:
    """**派发被拒**（不建 Task）→ 回执对象。

    `kind` ∈ `rejected_skill` / `rejected_artifact`；
    `available` 是平台列出来的"现在有什么"（能力 / 产物）。
    """
    return {
        "kind": kind,
        "sections": {"not_dispatched": detail, available_key(kind): available},
    }


#: 拒绝口 → 它列出的"现在有什么"那一段的段名 key
#:
#: ⚠️ ~~`rejected_turns`~~ **2026-11-03 删**：轮次不再由主 agent 点名（见
#: `briefing.turn_lines` 的分层说明），"点了不存在的轮次"这件事**结构上不可能**了。
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
    keys = HOPS["receipt"] if receipt.get("kind") not in _REJECTED_AVAILABLE else HOPS["receipt_rejected"]
    lines: list[str] = []
    sections = receipt.get("sections") or {}
    for key in keys:
        text = sections.get(key)
        if not text:
            continue
        # 🔴 **段名独占一行 → 来源另起一行 → 段体缩进 2 列**（`section_head` ＋ `body_lines`），
        # 和委托单**同一条规矩**。原来段体顶在 0 列：执行器那段长原话和【已做到】【还缺】
        # 这些平台框架话**左边界一模一样**，读的人分不清哪几行是执行器说的、哪几行是平台说的
        # —— 而"哪句是谁说的"正是这段存在的全部意义（用户 2026-11-02）。
        # 用户 2026-11-03 实测原话：*"return 那块日志还是很乱"*。
        lines += section_head(key) + body_lines(text, BODY)
    return "\n".join(lines)
