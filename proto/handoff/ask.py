"""**试答**（平台 → 主 agent）：执行器卡住时，平台**强制**先让主 agent 用自己的上下文试补。

> 🔴 **触发点变了**（2026-11-05，判定退休）：以前是**判定读正文猜**"它是在要信息" ✗，
> 现在是执行器**自己调 `ask_user` 申报** ✓ —— 所以这一步拿到的 `outcome.asked` 就是
> **它要什么**（工具参数里的**原话** ✓ 不用从正文里挑哪句是问题 ✓）。
> ⚠️ 判定时代那句"必须把已做到 / 还缺一起给它"随之作废 —— 那两样是判定吐的，没有来源了。

🔴 **三处都不截断**（用户 2026-11-02 的「交接三问」）：
这一步是个**独立的结构化调用**，它**只看得见这个提示词里的东西** ——
信息若落在长消息的后半段、或长任务描述里，它就答不上来，只能去打扰用户。
而"能不打扰用户就不打扰"正是这一步存在的全部意义。

⚠️ **别退回"最近 N 条"**：那是个**无声**的范围限制，比截断更难发现。
"""

from __future__ import annotations

from typing import Any


def build_prompt(skill_name: str, task: str, outcome: Any, messages: list) -> str:
    """试答那一步喂给主 agent 的**全部输入** —— 抽成纯函数是为了**可单测**。

    ⚠️ `messages` 渲染的是 `str(m.content)` —— **工具调用的参数不在里面**
    （`AIMessage.tool_calls` 的 `args`）。主 agent 自己写的 `delegate(task=…)` 全文、
    `read_artifact` 的 id 在这一步都看不见（2026-11-03 记档，见
    `docs/handoff-audit.md` §五 隐患 3）。**没有实测压到过，先不修。**
    """
    msgs = list(messages)
    history = "\n".join(f"{type(m).__name__}: {str(m.content)}" for m in msgs)
    return (
        f"你委派出去的工作卡住了，它在要信息。\n\n"
        f"技能：{skill_name}\n"
        f"你交给它的任务（**你写的** —— 可能补了用户没说的东西）：{task}\n"
        # 🔴 **要什么 = 工具参数里的原话**（结构 ✓）—— 不是从它的话里挑出来的
        f"**它要的信息**：{outcome.asked}\n"
        f"它最后说的那段（可能有上下文，也可能只是过场）：{outcome.report}\n\n"
        f"你的上下文（**全部 {len(msgs)} 条，全文**）：\n{history}\n\n"
        "请判断：**你的上下文里有没有它需要的信息？**\n"
        "- 有（用户之前说过）→ can_answer=true，answer 里给出补充内容，原话照搬\n"
        "- 没有 → can_answer=false，**不要编造**"
    )
