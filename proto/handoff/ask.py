"""**试答**（平台 → 主 agent）：执行器卡住时，平台**强制**先让主 agent 用自己的上下文试补。

> ⚠️ **必须把"已经做到什么程度 / 还缺什么"一起给它**（信息交接原则）——
> 只知道"它说缺课时"，主 agent 判不出该不该补；
> 知道"已完成学习目标和预习任务，只差当堂检测（需要课时才能写）"，
> 它就能判断这个信息在不在自己上下文里、值不值得去打扰用户。

🔴 **三处都不截断**（用户 2026-11-02 的「交接三问」）：
这一步是个**独立的结构化调用**，它**只看得见这个提示词里的东西** ——
信息若落在长消息的后半段、或长任务描述里，它就答不上来，只能去打扰用户。
而"能不打扰用户就不打扰"正是这一步存在的全部意义。

⚠️ **别退回"最近 N 条"**：那是个**无声**的范围限制，比截断更难发现。
"""

from __future__ import annotations

from typing import Any


def outcome_note(outcome: Any) -> str:
    """把判定给出的**进度 / 缺失项**渲染成附注 —— 所有"没完成"的出口都要带上。

    用户要求：*"尽可能暴露一些有用信息到外部"*。
    主 agent 看到"做到什么程度、还缺什么"，才能决定是**换个说法重派**还是**向用户说明**。
    """
    parts: list[str] = []
    if outcome.progress:
        parts.append(f"【已做到】（**判定说**）{outcome.progress}")
    if outcome.missing:
        parts.append("【还缺】（**判定说**）\n" + "\n".join(f"- {x}" for x in outcome.missing))
    return ("\n\n" + "\n".join(parts)) if parts else ""


def build_prompt(skill_name: str, task: str, outcome: Any, messages: list) -> str:
    """试答那一步喂给主 agent 的**全部输入** —— 抽成纯函数是为了**可单测**。

    ⚠️ `messages` 渲染的是 `str(m.content)` —— **工具调用的参数不在里面**
    （`AIMessage.tool_calls` 的 `args`）。主 agent 自己写的 `delegate(task=…)` 全文、
    `read_artifact` 的 id 在这一步都看不见（2026-11-03 记档，见
    `docs/handoff-audit.md` §五 隐患 3）。**没有实测压到过，先不修。**
    """
    msgs = list(messages)
    history = "\n".join(f"{type(m).__name__}: {str(m.content)}" for m in msgs)
    note = outcome_note(outcome)
    return (
        f"你委派出去的工作卡住了，它在请求信息。\n\n"
        f"能力：{skill_name}\n"
        f"你交给它的任务（**你写的** —— 可能补了用户没说的东西）：{task}\n"
        f"执行器说：{outcome.report}"
        f"{note}\n\n"
        f"你的上下文（**全部 {len(msgs)} 条，全文**）：\n{history}\n\n"
        "请判断：**你的上下文里有没有它需要的信息？**\n"
        "- 有（用户之前说过）→ can_answer=true，answer 里给出补充内容，原话照搬\n"
        "- 没有 → can_answer=false，**不要编造**"
    )
