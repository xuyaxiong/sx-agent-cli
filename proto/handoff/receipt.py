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
# ⚠️ **只 import 它真正用的**（2026-11-05 收敛渲染时清掉 `BODY` / `HOPS` /
#    `body_lines` / `section_head`）—— 渲染那四样现在归 `render_hop` 管，
#    这里再 import 一份就是"两处定义"的种子。
from proto.handoff import DELIVERY, render_hop

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


def next_note(kind: str, *, skill: str = "") -> str:
    """出口 → 回执的【下一步】那段话（`kind` ∈ `Receipt.kind`）。

    🔴 **为什么它住在这里、不写在调用点**（2026-11-05）：`docs/handoff-samples.md`
    是**生成的**（`experiments/render_handoffs.py`），它要渲染**同一份**文案 ——
    两处各写一遍，样例必然陈旧（实测就陈旧过：样例写「重派」、代码写「重新委派」）。
    一处定义，两边都从这儿取。

    ⚠️ 不认识的 `kind` → **KeyError**（当场炸）—— 别静默给一句错的处置。
    ⚠️ `skill`：只有 `completed` 那档用得上（判据以**技能声明的交付标准**为准，
    所以要能点出技能名 —— 见下面那段注释）。
    """
    if kind == "completed":
        # 🔴 **判据要说清**（2026-11-05 用户：*"产生结果依据又是啥啊 —— 这些都要能看到
        #    才能审计"*）：原来这里只有一句"够不够"，**从没说"够"的标准是什么** ——
        #    它只能看执行器自己说的话点头，判了也无据可依、更无从调优。
        #    标准**本来就有**：技能指令里写着交付格式（平台系统提示也是这么告诉它的：
        #    "不确定某个技能交付什么、按什么标准交付 → `read_skill` 读原文核对"）——
        #    这里只是把那条路**指出来**（平台不替它判，也不新造一套判据）。
        return (
            "**你判**：这次交付够不够？（不够就换个说法重新委派，或向用户说明）\n"
            f"判据以**技能「{skill}」声明的交付标准**为准"
            f"（不确定标准是什么就用 `read_skill('{skill}')` 读原文）——"
            "交付账就在上面【产出的产物】里。\n"
            "要核对内容就用 `read_artifact('<产物 ID>')` 读全文 —— 正文不在你的上下文里。\n"
            "⚠️ 它说的话在上面【执行器说】里，**过一眼** —— 它可能在话里说了什么"
            "（这次它没调 `ask_user`，平台不替它转达）。"
        )
    return _NEXT[kind]


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
    `available` 是平台列出来的"现在有什么"（技能 / 产物）。
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
    # 🔴 **2026-11-05：渲染收敛到 `handoff.render_hop`**（所有交接单共用一条路）——
    #    这里只负责挑"这一跳是哪一组段"（派发被拒那条走 `receipt_rejected`）。
    #    ⚠️ 收敛之前这里是**唯一一处"声明驱动"**的渲染（自己写了个循环遍历 `HOPS`）；
    #    现在那条路被抽成通用的，台账/试答/推回去/透传也走它。
    #    （原来那段注释留着它的来历：段体顶在 0 列时，执行器的原话和平台框架话
    #    **左边界一模一样**，读的人分不清哪句是谁说的 —— 用户 2026-11-03：
    #    *"return 那块日志还是很乱"*。）
    hop = "receipt_rejected" if receipt.get("kind") in _REJECTED_AVAILABLE else "receipt"
    return render_hop(hop, receipt.get("sections") or {})
