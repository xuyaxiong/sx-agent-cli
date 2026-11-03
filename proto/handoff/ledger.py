"""**台账**（平台 → 主 agent）：每轮开头把"派过什么、干到哪了"摆到它面前。

**只提供事实，不替它做决定**（"要不要重跑"是理解问题，该柔性）。

为什么要注入：不注入的话，主 agent 只看得到自己对话历史里那几条工具返回，
**不知道已经派过什么** —— 实测后果就是"用户补一句，它把三个任务全部重跑"（S14）。

🔴 **它是唯一"故意短"的一跳**：台账是**每轮重复注入的索引**，而全文另有出处 ——
那次的**回执**（`receipt.py`）就在对话历史里。所以这里选**显式截断**而不是渐进式披露
（渐进式披露要配一个"按需取"的工具，这里不需要新造）。

> **切了不标，是这两种做法里更糟的一种**：读的人分不清"它就说这么多"还是"被切了" ——
> 而这两种情况该做的决定**完全不同**。
"""

from __future__ import annotations

#: `Task.stage` 的全部取值 —— **与 `judgement._Judgement.status` 前三个同名**。
#: 唯一的映射是 `incomplete → failed`（判定说"还没做完"，推满轮次后任务记"失败"）。
#:
#: ⚠️ **这些是"要管"的状态**；`superseded` 是"不用管了"（同技能被重新委派时自动标记），
#: 所以台账里**不展开细节**，只列一行。
#:
#: 暂未实现的三个（评审人 C1 提过，先记下为什么不做、什么时候该做）：
#:   - `pending`                —— 建 Task 就立刻派发，没有"创建了还没跑"的窗口。
#:                                 等做「计划-执行分离」时再加
#:   - `waiting_for_dependency` —— `delegate` 是**同步**的，依赖在主 agent 循环里天然解决
#:                                 （它先调 A、拿到产物 ID、再调 B）。等做「异步委派」时再加
#:   - `cancelled`              —— 用户明确放弃时主动取消。需要给主 agent 新工具，
#:                                 且该场景尚未实测到。等真遇到再加
STAGE_MARK: dict[str, str] = {
    "running": "⏳ 进行中",
    "completed": "✅ 已完成",
    "waiting_for_input": "⚠️ 等用户答复",
    "failed": "❌ 失败",
    "superseded": "🔁 已被新的委派取代（不用管了）",
}

#: 需要**展开细节**（进度 / 缺失项 / 执行器原话）的状态
NEEDS_DETAIL = ("waiting_for_input", "failed")


def brief(text: str, limit: int, what: str = "长文本") -> str:
    """台账里的长文本 —— **要短就得说清"这是短的"**（用户 2026-11-02 的「交接三问」）。

    > "一定要注意交接的地方：① 交接了什么内容 ② **内容是否被截断了**
    >  ③ **过长的交接内容是否考虑了渐进式披露**。"

    这里选**显式截断**而不是渐进式披露，理由是**出处**：台账是**每轮重复注入**的索引，
    而全文另有出处 —— 那次的**回执**（`delegate` 的返回）就在对话历史里，
    主 agent 想看随时看得到。渐进式披露要配一个"按需取"的工具，这里不需要新造。

    🔴 **切了不标，是这两种做法里更糟的一种**：读的人分不清
    "它就说这么多"还是"被切了" —— 而这两种情况该做的决定**完全不同**
    （前者该换个说法重派，后者该去翻回执）。

    ⚠️ 顺手把换行压成空格：台账是**一行一条**的列表，多行值会把缩进结构撑坏。
    这不算内容损失（只动了空白），但**别在这里做别的加工**。
    """
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    # 🔴 **切了一刀就报一刀**（判据②：主动截断要有显式标记 + 说清全文在哪）。
    # 标记在**文本里**（读的人看得见），这一行在**日志里**（复盘的人看得见）—— 两者都要。
    from proto import trace

    trace.cut(f"台账·{what}", limit, len(text), see="那次的回执（`delegate` 的返回）")
    return f"{text[:limit]}……【平台截断：还有 {len(text) - limit} 字，全文见那次的回执】……"


def render_ledger(tasks: list[dict]) -> str:
    """把 Task 列表渲染成**给主 agent 的台账**（`node_prepare` 注入的那条 SystemMessage）。

    ⚠️ 调用方保证 `tasks` 非空（空的时候 `node_prepare` 直接不注入 —— 闲聊轮次里没有这段）。
    """
    lines: list[str] = []
    for t in tasks:
        mark = STAGE_MARK.get(t.get("stage", ""), t.get("stage", "?"))
        arts = f" → {', '.join(t['artifacts'])}" if t.get("artifacts") else ""
        lines.append(f"- {t['id']}  「{t['skill']}」  {mark}{arts}")
        # ⚠️ 没完成的任务，把**进度 / 缺失项**也带上（信息交接原则）——
        #    只说"没完成"，主 agent 没法决定是换个说法重派、还是向用户说明。
        if t.get("stage") in NEEDS_DETAIL:
            if t.get("progress"):
                lines.append(f"      已做到（判定说）：{brief(t['progress'], 150, '已做到')}")
            if t.get("missing"):
                lines.append(f"      还缺（判定说）：{brief('；'.join(t['missing']), 200, '还缺')}")
            lines.append(f"      执行器说：{brief(t['report'], 150, '执行器说')}")

    waiting = [t for t in tasks if t.get("stage") == "waiting_for_input"]
    # ⚠️ 这里**不再**叫主 agent 去重新委派 —— 待答的投递和转达都由平台做（见 `_feed_waiting`
    #    和 `node_finalize`）。主 agent 只需要别自己替用户编一个答案。
    hint = (
        "\n\n**「等用户答复」的那些，平台已经负责把问题转达给用户、也会把用户的答复投回去** ——"
        "你**不用**重新委派它们，更不要自己替用户编一个答案。"
        if waiting
        else "\n\n（用户补充信息时，**只重跑受影响的任务**，已完成且不受影响的不动。）"
    )
    return f"【平台提示】你已委派出去的工作（共 {len(tasks)} 项）：\n" + "\n".join(lines) + hint
