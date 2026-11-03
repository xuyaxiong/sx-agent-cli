"""**委托单**（平台 → 执行器）：Task 的结构 ＋ 渲染模板。

> 🔴 **为什么要有它**（2026-10-29，F15）：执行器**用完即弃**，每次投递都是一次全新的
> `run_task`，所以"这件事到目前为止的全部已知"必须由平台**重新交给它**。
> 原来三条路径（首跑 / 试答后重跑 / 答复投递）各拼各的，**用户历次答复被漏掉** ——
> 用户一次答一个时，答第二个就把第一个冲掉了，于是同一个问题问两遍。

⚠️ **Task 的每个字段要么渲染在这里、要么在 `evals/unit_briefing.py` 的排除名单里**
—— 那里有断言，以后加字段忘了渲染会红。

⚠️ "你上次说到哪"那块是**上一轮**的情况，措辞要写死这一点：判定也会看到这段，
别让它把上一轮的"还缺"当成这一轮还没做完。
"""

from __future__ import annotations

from typing import Any, TypedDict

from proto.handoff import SECTION

# ──────────────────────────── schema：委托单上有什么 ────────────────────────────


class Turn(TypedDict):
    """**一轮用户原话**（平台硬搬的快照，不经任何转述）。

    🔴 `after` 是**紧邻的前一条 AI 消息**（纯位置关系，平台搬，不判断语义）——
    用户答「A」时，脱离它回答的那句（选项列表在里面）就没意义。
    """

    n: int
    text: str
    after: str


class QA(TypedDict):
    """**一问一答成对** —— 免得「答」脱离了「问」。

    `report` 是**覆盖**写的（`absorb_outcome`），所以只存答案的话，
    第二次重入之后旧答案就成了没有指涉的孤儿。问题由**平台**在收下答案的那一刻
    从 `report` 原样截下，不靠任何 LLM 转述。
    """

    by: str  # "用户" | "主 agent"
    q: str  # 执行器问的原话
    a: str  # 答复


class Task(TypedDict, total=False):
    """**委托单**：一次委派出去的工作的全部已知。

    ⚠️ `total=False` 是**如实**：`case.initial_tasks` 注入的 Task 可能只有部分字段
    （见 `evals/_child.py`）。**没有运行时校验** —— 靠 `delegate` 建全 + 断言兜底。
    """

    id: str
    skill: str
    task: str  # 主 agent 写的"这次要什么"（**它说的，可能加了料**）
    stage: str  # running / completed / waiting_for_input / failed / superseded
    artifacts: list[str]  # **产出**的产物 ID
    refs: list[str]  # **输入引用**的产物 ID（两个方向，别混）
    report: str  # 执行器最后一轮的原话（**覆盖写**）
    progress: str  # 判定说：已做到什么程度
    missing: list[str]  # 判定说：还缺什么
    qa: list[QA]
    user_turns: list[Turn]  # 派发那一刻的快照
    based_on: list[int]  # 主 agent 点名的轮次（那几轮给全文）
    agent_tried: bool  # 平台强制试答过没有


# ──────────────────────────── 渲染：注入前那一瞬才拼 ────────────────────────────


#: 清单里每轮露出的开头字数（`_one_line` 的默认 limit）—— 报"切了多少"时要用同一个数
_one_line_limit = 40


def _flat(text: object) -> str:
    """`_one_line` **压成一行**之后的样子 —— 判"切没切"必须用**同一个口径**。

    ⚠️ 分开写是有原因的：`render_briefing` 报"切了几轮"时要**先按这个口径压平再比长度**，
    否则换行多的短消息会被误判成"超长"。
    """
    return " ".join(str(text or "").split())


def _one_line(text: str, limit: int = _one_line_limit) -> str:
    """压成一行 + 截到 limit 字 —— 清单里**一行一轮**，不能让它撑成多行。

    ⚠️ 这里**不逐条记日志**（一轮两处、5 轮就 10 行，太吵）——
    由 `render_briefing` 报一次合计（见那里的 `trace.cut`）。
    """
    flat = _flat(text)
    return flat if len(flat) <= limit else flat[:limit] + "…"


def render_briefing(task: dict) -> str:
    """把 Task 渲染成**执行器的输入** —— 所有重入路径的唯一入口。

    ⚠️ 名字是 task 不是 given：它是**整份交接**（任务 / 上次做到哪 / 历次答复 /
    已产出产物 / 可读产物），不是"这一轮的增量"—— 执行器用完即弃，每次都是重入。
    """
    from proto.artifacts import ARTIFACTS

    lines = [
        # 🔴 **来源标注**（用户 2026-11-02）：执行器必须能分清"**哪句是用户说的、
        # 哪句是模型说的**"。平台**不阻止**主 agent 补充信息（那是它的活），
        # 但**必须让下游知道这是谁的话** —— 否则模型补的东西会被当成用户的事实。
        # 实测（b3-smoke2）：执行器把主 agent 编的"三年级/四年级"说成了
        # "**您写的是**三年级/四年级" —— 而用户从来没写过。
        f"{SECTION['task']}（**主 agent 交代的** —— 它根据对话写的，可能补了用户没说的东西）\n"
        f"{task.get('task', '')}"
    ]

    # ── 用户原话（平台硬搬，未经转述）—— 渐进式披露：清单常驻、全文按需 ──
    turns = task.get("user_turns") or []
    if turns:
        picked = set(task.get("based_on") or [])
        block = [f"{SECTION['user_turns']}（**用户说的** —— 平台直接搬，未经转述）"]
        for t in turns:
            n, text = t.get("n"), str(t.get("text") or "")
            # 🔴 **短应答必须带上它在回答什么**（用户 2026-11-02）：光一个「A」，
            # 执行器既解读不了、也核对不了主 agent 的转述 —— 而这一段存在的
            # 全部理由就是**对冲转述失真**。见 `agent._user_turns` 的 docstring。
            after = str(t.get("after") or "").strip()
            ctx = f"\n  ↳ **它在回答的上一句**：「{_one_line(after)}」" if after else ""
            if n in picked:
                # 主 agent **点名**的那几轮 → 直接给全文（点名本身就是一次"按需取"）
                block.append(f"- **第 {n} 轮（{len(text)} 字）—— 本次工作依据的就是它**：\n{text}{ctx}")
            else:
                block.append(f"- 第 {n} 轮（{len(text)} 字）：{_one_line(text)}{ctx}")
        block.append(
            "（**这是事实来源**；【任务】是这次的要求，两者冲突时**以原话为准**。"
            "要看哪一轮的**完整上下文**（它回答的上一句 ＋ 原话）就用 `read_user_turn(轮次)` 读，"
            "不要凭那几十个字猜。）"
        )
        lines.append("\n".join(block))
        # 🔴 **切了几轮就报几轮**（判据②）。逐轮报太吵（一轮两处、5 轮就 10 行），
        # 所以这里报**一次合计** —— 数字由产出方自己算，**不靠事后解析事后文本**。
        #
        # ⚠️ **只算真的被切了的**：短于 40 字的轮次是**整段给出**的（`_one_line` 原样返回），
        # 把它算进去就会报出"保留 40 字 / 原文 13 字"这种**自相矛盾**的数
        # （2026-11-03 实测踩到，`handoff-verify-7` 的日志里）。
        unpicked = [t for t in turns if t.get("n") not in picked]
        cut_src = [_flat(t.get("text")) for t in unpicked]
        cut_src += [_flat(t.get("after")) for t in turns if t.get("after")]
        cut = [x for x in cut_src if len(x) > _one_line_limit]
        if cut:
            from proto import trace

            trace.cut(
                "交接书·用户原话清单（含「它在回答的上一句」）",
                _one_line_limit * len(cut),
                sum(len(x) for x in cut),
                see="`read_user_turn(轮次)`（执行器自己读，含「它在回答的上一句」）",
            )

    progress = (task.get("progress") or "").strip()
    missing = [m for m in (task.get("missing") or []) if m]
    report = (task.get("report") or "").strip()
    if progress or missing or report:
        # 🔴 **子段也要标来源**（用户 2026-11-02）：这一段里混着**两方**的话 ——
        # 「已做到 / 还缺」是**判定**说的（第三方评审，它可能判错），
        # 「你上次说」才是**执行器自己**的话。原来笼统写成"你上次说到哪"，
        # 读的人分不出哪句是谁说的 —— 而这两者的可信度**根本不是一回事**。
        block = [f"{SECTION['last_round']}（**这是上一轮的情况，不是本轮**）"]
        if progress:
            block.append(f"- **判定说**已做到：{progress}")
        if missing:
            block.append("- **判定说**还缺：\n" + "\n".join(f"  - {m}" for m in missing))
        if report:
            block.append(f"- **你上次说**：\n{report}")
        lines.append("\n".join(block))

    # 🔴 **一问一答成对**（用户 2026-11-02）：`report` 是**覆盖**写的
    # （`absorb_outcome` 无条件覆盖），所以只存答案的话，第二次重入之后
    # 旧答案就成了**没有指涉的孤儿** —— 那正是修过的"答第二个把第一个冲掉"
    # 的**镜像版**（那次修的是答案，这次是问题）。问题由**平台**在收下答案的
    # 那一刻从 `report` 原样截下，不靠任何 LLM 转述。
    qa = [x for x in (task.get("qa") or [])
          if isinstance(x, dict) and str(x.get("a") or "").strip()]
    if qa:
        block = [f"{SECTION['answered']}（**一问一答成对** —— 免得「答」脱离了「问」）"]
        for x in qa:
            q = str(x.get("q") or "").strip()
            by = str(x.get("by") or "用户")
            if q:
                # `q` 是执行器的**整条 report**（可能多行，前面还有一句交代）——
                # 续行要缩进对齐，否则"答"和它回答的那句"问"会被冲散（实测踩过）。
                # ⚠️ **不抽取"哪句才是问题"**：那是语义判断，平台不做（见 §0.7）。
                q_lines = q.splitlines() or [q]
                block.append(f"- **执行器问**：{q_lines[0]}")
                block += [f"  {ln}" if ln.strip() else "  " for ln in q_lines[1:]]
                block.append(f"  **{by}** 答：{x['a']}")
            else:
                block.append(f"- **{by}** 答：{x['a']}")
        if len(qa) > 1:
            block.append("（如前后答复冲突，**以后面的为准**）")
        lines.append("\n".join(block))

    def _index(aids: list[str]) -> list[str]:
        return [
            f"- {aid}《{ARTIFACTS[aid]['title']}》（{len(ARTIFACTS[aid]['content'])} 字）"
            f"—— {ARTIFACTS[aid].get('summary') or '（无梗概）'}"
            for aid in aids
            if aid in ARTIFACTS
        ]

    produced = _index([a for a in (task.get("artifacts") or []) if a])
    if produced:
        lines.append(
            f"{SECTION['already_produced']}\n"
            + "\n".join(produced)
            + "\n（这次要**在它基础上补全**，提交完整版；不要从零重写。）"
        )

    refs = _index([a for a in (task.get("refs") or []) if a])
    if refs:
        lines.append(
            f"{SECTION['readable']}\n"
            + "\n".join(refs)
            + "\n（这是清单，不是正文。要用哪一份就用 read_artifact 读全文，不要凭标题猜。）"
        )

    if progress or missing or report or qa:
        # 只有**重入**才说这句：首跑说"接着干"会让它以为有前情
        lines.append(f"{SECTION['resume']}\n接着已有的工作继续，**不要重复已完成的工作**。")
    return "\n\n".join(lines)


def absorb_outcome(rec: dict, outcome: Any) -> None:
    """把执行器的结果**写进 Task** —— 四个字段一个都不能少（F15 的同类：存了才可能喂）。

    ⚠️ `artifacts` 只在非空时覆盖：执行器可能这一轮没产出，不能把上一轮的抹掉。
    """
    rec["report"] = outcome.report
    rec["progress"] = outcome.progress
    rec["missing"] = list(outcome.missing)
    if outcome.artifacts:
        rec["artifacts"] = list(outcome.artifacts)
