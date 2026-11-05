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

from proto.handoff import BODY, INJECTED, body_lines, render_hop

#: 消息类型 → **人话**（读的人是主 agent，所以 `AIMessage` 是"你"）。
#:
#: 🔴 **原来是 `f"{type(m).__name__}: …"`** —— `HumanMessage:` / `AIMessage:` 这种
#: **框架的 Python 类名直接漏给模型**（2026-11-05 审核）。别处都小心地渲染成人话
#: （交接书里是"**用户说的**" / "**它说的**"），只有这一处漏了。
_WHO: dict[str, str] = {
    "HumanMessage": "用户",
    "AIMessage": "你",
    "ToolMessage": "工具返回",
    "SystemMessage": "平台",
}


def _who(m: Any) -> str:  # noqa: ANN401
    """这条消息**是谁说的** —— 打过注入标记的（平台透传）不叫"你说的"。

    ⚠️ `injected_by` 是**结构**（挂在 `additional_kwargs` 上，不漏给模型）——
    这里读它，**不是**"看内容像不像平台的话"（那是反结构化，判据一）。
    """
    if (getattr(m, "additional_kwargs", None) or {}).get("injected_by") in INJECTED:
        return "**平台转达**"
    return f"**{_WHO.get(type(m).__name__, type(m).__name__)}**"


def _item(m: Any) -> str:  # noqa: ANN401
    """一条消息 → **一个列表项**（多行的整块缩在下一层）。

    🔴 **2026-11-05 修**：原来是 `f"- {_who(m)}：{str(m.content)}"` —— 单行的没问题，
    但**工具返回 / 平台注入本身就是一张单子**（回执有【交付】【它要的信息】【执行器说】
    【下一步】，台账有【平台提示】）。那一行会把这张单子的**第一行粘在列表项后面**，
    其余行按**原文的缩进**跟着走 —— 于是单子的段名（原文 0 列）落回**和 `- ` 同一列**：

        - **工具返回**：【交付】        ← 段名被粘住
          它在**要信息**（调了 `ask_user`）
        【它要的信息】                 ← 看着像【你的上下文】自己的一段 ✗
          来源：…

    实测后果：**同一个提示词里 `【它要的信息】` 出现两次**（顶层是这一问、回执里是上一问），
    而层级看不出来，读的人分不清哪个是哪个。
    这和 `executor.py` 的 `task_text` 修过的是**同一个形状**（当时那句：
    *"【已知信息】和它里面的【任务】【用户原话】同在 0 列"*）——
    **一张单子要么整块在下一层，要么别塞进来。**
    """
    body = str(m.content)
    lines = body.splitlines() or [""]
    if len(lines) == 1:
        return f"- {_who(m)}：{lines[0]}"
    # 段名那一行**不跟内容** —— 整张单子缩在下一层（`render_hop` 还会再 +2）
    return "\n".join([f"- {_who(m)}：", *body_lines(body, BODY)])


def build_prompt(skill_name: str, task: str, outcome: Any, messages: list) -> str:
    """试答那一步喂给主 agent 的**全部输入** —— 抽成纯函数是为了**可单测**。

    ⚠️ **2026-11-05：走唯一那条渲染路**（`render_hop`）。在这之前它是**平铺的
    `字段：值`**：没有段名、没有缩进、`请判断` 夹在最后和上面那堆事实没有分隔 ——
    用户原话：*"做决策的交接单就不好"*。现在它和委托书/回执是同一套体例：
    段名 0 列 · 来源 2 列 · 段体 2 列。
    ⚠️ `asked` / `task` / `report` **三段和回执/委托书共用**（同一个语义一处定义）。

    ⚠️ `skill_name` **跟着【任务】走**（"派给「lesson-prep」"那一行）——
    它属于"这次要什么"，不单起一段；但**不能丢**（交接三问①：交接了什么）。
    这一步是**独立调用**，模型手里没有 `delegate` 的工具上下文 ——
    不写它，模型就不知道派给了谁。

    ⚠️ `messages` 渲染的是 `str(m.content)` —— **工具调用的参数不在里面**
    （`AIMessage.tool_calls` 的 `args`）。主 agent 自己写的 `delegate(task=…)` 全文、
    `read_artifact` 的 id 在这一步都看不见（2026-11-03 记档，见
    `docs/handoff-audit.md` §五 隐患 3）。**没有实测压到过，先不修。**
    """
    msgs = list(messages)
    history = "\n".join(_item(m) for m in msgs)
    return render_hop("ask", {
        # 🔴 **要什么 = 工具参数里的原话**（结构 ✓）—— 不是从它的话里挑出来的
        "asked": str(getattr(outcome, "asked", "") or "").strip(),
        "task": f"（委派给「{skill_name}」）\n{str(task or '').strip()}",
        "report": str(getattr(outcome, "report", "") or "").strip(),
        # ⚠️ "全部 N 条、全文、不截断" 写在**段体**里 —— 它是内容的一部分，
        #    不是"来源"（来源那一行回答的是"这句是谁说的"，见 `SOURCE_NOTE`）。
        "ask_context": f"（全部 {len(msgs)} 条，全文，不截断）\n{history}",
        "ask_decide": (
            "你的上下文里**有没有它需要的信息**？\n"
            "- 有（用户之前说过）→ `can_answer=true`，`answer` 里给出补充内容，**原话照搬**\n"
            "- 没有 → `can_answer=false`，**不要编造**"
        ),
    })
