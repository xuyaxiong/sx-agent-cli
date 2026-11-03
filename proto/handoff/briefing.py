"""**委托单**（平台 → 执行器）：Task 的结构 ＋ 渲染模板。

> 🔴 **为什么要有它**（2026-10-29，F15）：执行器**用完即弃**，每次投递都是一次全新的
> `run_task`，所以"这件事到目前为止的全部已知"必须由平台**重新交给它**。
> 原来三条路径（首跑 / 试答后重跑 / 答复投递）各拼各的，**用户历次答复被漏掉** ——
> 用户一次答一个时，答第二个就把第一个冲掉了，于是同一个问题问两遍。

⚠️ **Task 的每个字段要么渲染在这里、要么在 `evals/unit_briefing.py` 的排除名单里**
—— 那里有断言，以后加字段忘了渲染会红。

## 段的顺序 ＝ **读的人理解这件事的顺序**（用户 2026-11-03）

    我要做什么 → 用户原话 → 我做过什么 / 问过什么 → 判定怎么看 → 我产出了什么 → 接着干什么

⚠️ 原来是**按来源分段、且最新在前**：`report`（它最新那句）排在 `qa`（更早的历次问）
**前面**，读的人得在两段之间来回跳，才能把一件事的经过拼起来 ——
实测（`教案日志.txt` R07）读起来是**倒的**。现在合成**一条时间序**。

## 两处硬机制（确定性，不靠措辞）

**① 排版** —— 段名 0 列 / 段体 2 列 / 每层 +2（`proto.handoff.BODY` ＋ `body_lines`）。
治的是实测三种破法，见 `proto/handoff/__init__.py` 那段注释。

**② 去重** —— `qa` 里的「问」是**当时那条 `report`** 原样抄下来的
（`graph._feed_waiting` / `agent.delegate` 两处都这么建），而 `report` 要到
`absorb_outcome` 才被覆盖，**渲染发生在覆盖之前** —— 所以

    "最后一问 == report" 在**每一次重入渲染时恒成立**

两边都渲染 = 同一段话贴两遍（实测 R07：275 字，占交接书 11%）。
🔴 **只折叠逐字相同的最后一条，且必须标出来** —— 悄悄不贴和悄悄切一刀一样糟：
读的人分不出"它没说"和"被折叠了"。折叠的**字数**另报一行日志（同 `trace.cut` 的做法）。

⚠️ **判定也会看到这段**：措辞要写死"这是**上一轮**的情况"，
别让它把上一轮的"还缺"当成这一轮还没做完。
"""

from __future__ import annotations

from typing import Any, TypedDict

from proto.handoff import BODY, body_lines, bullet_lines, section_head

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

    ⚠️ **渲染出来的就是全部**：没有"给人看的另一份"（用户 2026-11-03：
    *"你显示给我看的，就是传给 llm 看的"*）—— 日志里 `📥 INJECT` dump 的是
    这个函数的返回值本身，不是它的改写。
    """
    from proto.artifacts import ARTIFACTS

    blocks: list[str] = []

    # ── ① 任务：这次要什么 ──────────────────────────────────────────────
    # 🔴 **来源标注**（用户 2026-11-02）：执行器必须能分清"**哪句是用户说的、
    # 哪句是模型说的**"。平台**不阻止**主 agent 补充信息（那是它的活），
    # 但**必须让下游知道这是谁的话** —— 否则模型补的东西会被当成用户的事实。
    # 实测（b3-smoke2）：执行器把主 agent 编的"三年级/四年级"说成了
    # "**您写的是**三年级/四年级" —— 而用户从来没写过。
    blocks.append("\n".join([*section_head("task"), *body_lines(task.get("task", ""), BODY)]))

    # ── ② 用户原话（平台硬搬，未经转述）—— 渐进式披露：清单常驻、全文按需 ──
    turns = task.get("user_turns") or []
    if turns:
        picked = set(task.get("based_on") or [])
        block = section_head("user_turns")
        for t in turns:
            n, text = t.get("n"), str(t.get("text") or "")
            # 🔴 **短应答必须带上它在回答什么**（用户 2026-11-02）：光一个「A」，
            # 执行器既解读不了、也核对不了主 agent 的转述 —— 而这一段存在的
            # 全部理由就是**对冲转述失真**。见 `agent._user_turns` 的 docstring。
            after = str(t.get("after") or "").strip()
            if n in picked:
                # 主 agent **点名**的那几轮 → 直接给全文（点名本身就是一次"按需取"）
                block.append(f"{BODY}- **第 {n} 轮（{len(text)} 字）—— 本次工作依据的就是它**：")
                # ⚠️ 原文**整块缩进**（原来顶在 0 列，跟它上面那个 `-` 对不齐）
                block += body_lines(text, BODY * 2)
            else:
                block.append(f"{BODY}- 第 {n} 轮（{len(text)} 字）：{_one_line(text)}")
            if after:
                # `↳` 对齐到**文字**（`- ` 之后），所以点名轮和清单轮看起来是同一个东西
                block.append(f"{BODY}  ↳ **它在回答的上一句**：「{_one_line(after)}」")
        block.append(
            f"{BODY}（**这是事实来源**；【任务】是这次的要求，两者冲突时**以原话为准**。"
            "要看哪一轮的**完整上下文**（它回答的上一句 ＋ 原话）就用 `read_user_turn(轮次)` 读，"
            "不要凭那几十个字猜。）"
        )
        blocks.append("\n".join(block))
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

    # ── ③ 问答经过：**一条时间序**（历次问→答，末尾接上"它最后说的那句"）──
    #
    # 🔴 **一问一答成对**（用户 2026-11-02）：`report` 是**覆盖**写的
    # （`absorb_outcome` 无条件覆盖），所以只存答案的话，第二次重入之后
    # 旧答案就成了**没有指涉的孤儿** —— 那正是修过的"答第二个把第一个冲掉"
    # 的**镜像版**（那次修的是答案，这次是问题）。问题由**平台**在收下答案的
    # 那一刻从 `report` 原样截下，不靠任何 LLM 转述。
    qa = [x for x in (task.get("qa") or [])
          if isinstance(x, dict) and str(x.get("a") or "").strip()]
    report = (task.get("report") or "").strip()
    # 🔴 **确定性去重**（判据：见模块 docstring ②）—— 逐字比较两个**我们自己写的字段**，
    # 不是从散文里反结构化。成立与否是"恒成立"而不是"碰巧"，所以能断言。
    folded = bool(qa) and bool(report) and str(qa[-1].get("q") or "").strip() == report
    if qa or report:
        block = section_head("answered")
        for i, x in enumerate(qa, 1):
            q = str(x.get("q") or "").strip()
            by = str(x.get("by") or "用户")
            if q:
                # `q` 是执行器的**整条 report**（可能多行，前面还有一句交代）。
                # ⚠️ **不抽取"哪句才是问题"**：那是语义判断，平台不做（见 §0.7）。
                block.append(f"{BODY}- **问 {i}**（执行器原话）：")
                block += body_lines(q, BODY * 2)
            else:
                block.append(f"{BODY}- **问 {i}**：（这一轮它没留下话）")
            # ⚠️ 「答」是**和「问」平级的一项**，不是「问」的续行 ——
            # 原来是 `  **用户** 答：`（和问的续行同一个缩进），答看起来像问的一部分。
            block.append(f"{BODY}- **答 {i}**（**{by}**）：")
            block += body_lines(str(x["a"]), BODY * 2)
        if folded:
            block.append(
                f"{BODY}（**它最后说的那句**就是上面「问 {len(qa)}」，逐字相同 —— "
                "原样，**不重复贴**。）"
            )
        elif report:
            # 兜底：`report` 不是任何一问（首跑 / 试答后重跑那两条路）——
            # **一个字都不能少**，所以照原样给它一块。
            block.append(f"{BODY}- **它最后说的那句**（原样，这一轮还没有人答）：")
            block += body_lines(report, BODY * 2)
        if len(qa) > 1:
            block.append(f"{BODY}（如前后答复冲突，**以后面的为准**）")
        blocks.append("\n".join(block))
        if folded:
            # 折叠**也要报一刀**（同 `trace.cut`）：标记在**文本里**（读的人看得见），
            # 这一行在**日志里**（复盘的人看得见）—— 两者都要有。
            from proto import trace

            trace.note(
                f"🔁 折叠重复 · 交接书：它最后说的那句 == 上面「问 {len(qa)}」"
                f"（{len(report)} 字，原样不重复贴）"
            )

    # ── ④ 上次判定：**第三方评审**的话（和上面两段的"当事人原话"不是一回事）──
    progress = (task.get("progress") or "").strip()
    missing = [m for m in (task.get("missing") or []) if m]
    if progress or missing:
        # 🔴 **子段也要标来源**（用户 2026-11-02）：「已做到 / 还缺」是**判定**说的
        # （第三方评审，它可能判错）—— 而"它上次说的那句"已经按时间序挪进【问答经过】，
        # 这一段现在**只有判定的话**，所以段名就叫【上次判定】、来源标注一次写死。
        block = section_head("last_round")
        if progress:
            block.append(f"{BODY}- 已做到：")
            block += body_lines(progress, BODY * 2)
        if missing:
            block.append(f"{BODY}- 还缺：")
            for m in missing:
                block += bullet_lines(m, BODY * 2)
        blocks.append("\n".join(block))

    def _index(aids: list[str]) -> list[str]:
        return [
            f"- {aid}《{ARTIFACTS[aid]['title']}》（{len(ARTIFACTS[aid]['content'])} 字）"
            f"—— {ARTIFACTS[aid].get('summary') or '（无梗概）'}"
            for aid in aids
            if aid in ARTIFACTS
        ]

    produced = _index([a for a in (task.get("artifacts") or []) if a])
    if produced:
        blocks.append("\n".join([
            *section_head('already_produced'),
            *[f"{BODY}{x}" for x in produced],
            f"{BODY}（这次要**在它基础上补全**，提交完整版；不要从零重写。）",
        ]))

    refs = _index([a for a in (task.get("refs") or []) if a])
    if refs:
        blocks.append("\n".join([
            *section_head('readable'),
            *[f"{BODY}{x}" for x in refs],
            f"{BODY}（这是清单，不是正文。要用哪一份就用 read_artifact 读全文，不要凭标题猜。）",
        ]))

    if progress or missing or report or qa:
        # 只有**重入**才说这句：首跑说"接着干"会让它以为有前情
        blocks.append("\n".join([
            *section_head("resume"),
            f"{BODY}接着已有的工作继续，**不要重复已完成的工作**。",
        ]))
    return "\n\n".join(blocks)


def absorb_outcome(rec: dict, outcome: Any) -> None:
    """把执行器的结果**写进 Task** —— 四个字段一个都不能少（F15 的同类：存了才可能喂）。

    ⚠️ `artifacts` 只在非空时覆盖：执行器可能这一轮没产出，不能把上一轮的抹掉。
    """
    rec["report"] = outcome.report
    rec["progress"] = outcome.progress
    rec["missing"] = list(outcome.missing)
    if outcome.artifacts:
        rec["artifacts"] = list(outcome.artifacts)
