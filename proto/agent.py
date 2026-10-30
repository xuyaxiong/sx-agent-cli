"""主 agent —— **一个完整的 agent**（有自己的工具与循环），不是纯路由器。

三种出路（**由它自己决定**）：
  1. 直接答              —— 闲聊、追问、澄清（**不建 Task**）
  2. 自己产出短内容       —— write_artifact（**不建 Task**）
  3. `delegate` 委派      —— 需要专业能力时（**建 Task**）

**`delegate` 内部由平台保证四件事**（LLM 跳不过）：

    ⓪ **取代旧的**：同 skill 下未了结的旧 Task → 标记 `superseded`（免得台账挂过期项）
    ① **执行器干活**（`run_task`，隔离上下文；内部含"出口限制"，最多 react 3 轮）
    ② **先让主 agent 试答**：用它自己的完整上下文 + 进度 + 缺失项；答得上就不打扰用户
    ③ **登记待答**：答不上才 `stage = waiting_for_input`，交给主 agent 去问用户

**Task 只在决定委派时才建。** 不委派就没有 Task。
"""

from __future__ import annotations

import time
from typing import Any

from langchain.agents import create_agent
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from proto import trace
from proto.config import get_model
from proto.executor import run_task
from proto.llm import safe_structured
from proto.artifacts import (  # 产物领域
    ARTIFACTS,
    artifact_index_lines,
    list_artifacts,
    read_artifact,
    write_artifact,
)
from proto.skill_pack import list_references_tool, read_reference  # 参考文件（磁盘）
from proto.skills import SKILLS, read_skill  # 技能领域

#: 主 agent 的 system prompt —— 平台约定，**不约束 skill 作者**
_MAIN_PROMPT = (
    "你是一个能自己干活的助手，**不必把所有事都委派出去**。\n"
    "\n"
    "两种做法：\n"
    "1. **直接答**：闲聊、追问、解释、澄清、简单修改——不需要专业能力的事，你自己说。\n"
    "2. **delegate 委派**：需要下面列出的**专业能力**时，**一律委派**，不要自己凭感觉做。\n"
    "   执行器在独立上下文里按该能力的指令干活，只把产物和摘要带回来。\n"
    "\n"
    "为什么必须委派：专业能力的产出可能很长，自己干会把你的上下文撑爆。\n"
    "**你在派发前无法判断产出有多长**，所以不要猜——需要专业能力就委派。\n"
    "\n"
    "委派回来如果显示「没完成，它在请求信息」——**什么都别做，尤其不要自己补一个答案**：\n"
    "平台已经替你查过一次上下文了（`delegate` 内部做的）；确实没有，\n"
    "平台会负责把问题**转达给用户**，用户答复后也会**投回给那个执行器**。\n"
    "你这条回复只要说清楚「派了什么、还差什么」就行。\n"
    "\n"
    "🔴 **绝不要替用户定一个答案再委派一次。** 用户没说过的，就是没有 ——\n"
    "你猜的那个值会变成一份基于错误前提的成品，而用户不知道。\n"
    "\n"
    "## 信息不全时，**不要一律问** —— 先判断能不能推进\n"
    "\n"
    "- 能推进就**先推进**（真的缺东西时，执行器会告诉你）；\n"
    "- 确实推进不了的，平台会问；你不用抢着问，也不用挤牙膏；\n"
    "- **哪些参数是必答的、哪些有默认值，以技能指令为准** —— 平台不替你猜，你也别猜。\n"
    "\n"
    "## 🔴 产出范围**以用户明确说的为准**，不要自行扩大\n"
    "\n"
    "用户说「帮我备课」就**只出教案**；不要说「我顺便把导学单和小测也做了」。\n"
    "**用户没要的，不要直接做掉**——那会浪费他的时间和成本。\n"
    "\n"
    "想提供的话，**在回复末尾提一句**就行（如「需要的话我也可以出导学单和随堂小测」），\n"
    "让用户自己决定要不要。\n"
    "\n"
    "同理，用户说「备课、导学单、小测」就是**三样**，不要多也不要少。\n"
    "\n"
    "## 🔴 前序产物怎么交给下一个任务\n"
    "\n"
    "**传 ID，不要转述正文，也不用先读它。** 需要前面交付的产物当输入时：\n"
    "`delegate(skill_name=\"...\", task=\"...\", artifacts=[\"art_001\"])`\n"
    "执行器会拿到它的名称与梗概，需要全文**它自己读**。\n"
    "\n"
    "- **别读**：转交之前先 `read_artifact` 读一遍，等于把那几千字**永久背进你的上下文**\n"
    "  （读一次就一直在），隔离就白做了 —— 而你并不需要它。\n"
    "- **别抄**：`task` 只写「这次要什么」（课题、学情、用途、约束），\n"
    "  **不要写「内容是什么」** —— 内容由执行器从产物里读，你复述一遍既失真又多余。\n"
    "- 不确定有哪些产物 → `list_artifacts()`（只有 ID、名称、字数、梗概，很便宜）。\n"
    "- 不确定某个能力**交付什么、按什么标准交付** → `read_skill(name)` 读原文核对，\n"
    "  **不要凭一句话描述猜**：你猜出来的要求会压过能力自己的交付标准。\n"
    "- 传了不存在的产物 ID 会被当场拒绝，并告诉你现有的清单。\n"
    "\n"
    "## 🔴 回复用户时的硬要求\n"
    "\n"
    "**产物正文不要搬进你的回复。** 委派完成后，只告诉用户这四样：\n"
    "交付了哪几样、各自的产物 ID、**文件在哪**、以及执行器给的一句话摘要。\n"
    "\n"
    "- 🔴 **文件路径一定要给** —— `delegate` 的返回里带着它（形如"
    "「文件：art_001 → <路径>」）。\n"
    "  用户是拿着这个路径去**打开文件**的：**只说 ID，他找不到东西**。\n"
    "  （实测：路径已经给到你了，但你有时会漏掉它、只报 ID 和摘要 —— 别漏。）\n"
    "\n"
    "- **不要**用 `read_artifact` 把产物正文读出来贴进回复——产物是给用户看的卡片/文件，\n"
    "  正文贴进来会**撑爆你的上下文**，隔离就白做了。\n"
    "- **不要**自己用 `write_artifact` 重新写一遍执行器已经交付的内容。\n"
    "- `read_artifact` 是**按需**能力（渐进式披露第二层）：用户明确说「给我看看内容」\n"
    "  「改一下某处」、或你自己确实要核对内容时才读。**转交给下一个任务时不要读** ——\n"
    "  传 ID 就够，执行器自己会读。\n"
    "\n"
    "`write_artifact` 只用于**你自己产出**的短内容（比如一段说明、一份对比表）；\n"
    "你自己产出时同样要给**名称、一句话梗概、文件类型、正文**四样，\n"
    "交付后照上面那条把**文件路径**告诉用户。"
)


# ──────────────────────────── 主 agent 试答 ────────────────────────────


class _AgentAnswer(BaseModel):
    can_answer: bool = Field(description="你的上下文里有没有它缺的信息")
    answer: str = Field(default="", description="can_answer=true 时给出补充内容，原话照搬不要改写")


def _ask_main(runtime: ToolRuntime, skill_name: str, task: str, outcome: Any) -> str:
    """**平台强制的一步**：让主 agent 用自己的上下文试着补上缺的信息。

    ⚠️ **必须把"已经做到什么程度 / 还缺什么"一起给它**（信息交接原则）——
    只知道"它说缺课时"，主 agent 判不出该不该补；
    知道"已完成学习目标和预习任务，只差当堂检测（需要课时才能写）"，
    它就能判断这个信息在不在自己上下文里、值不值得去打扰用户。
    """
    history = "\n".join(
        f"{type(m).__name__}: {str(m.content)[:300]}"
        for m in (runtime.state.get("messages") or [])[-10:]
    )
    note = _outcome_note(outcome)
    prompt = (
        f"你委派出去的工作卡住了，它在请求信息。\n\n"
        f"能力：{skill_name}\n"
        f"你交给它的任务：{task}\n"
        f"执行器说：{outcome.report}"
        f"{note}\n\n"
        f"你的上下文（最近的对话）：\n{history}\n\n"
        "请判断：**你的上下文里有没有它需要的信息？**\n"
        "- 有（用户之前说过）→ can_answer=true，answer 里给出补充内容，原话照搬\n"
        "- 没有 → can_answer=false，**不要编造**"
    )
    # 调用失败时保守当作「答不上」—— 交给用户，而不是编一个答案
    r = safe_structured(
        _AgentAnswer, prompt, _AgentAnswer(can_answer=False), tag="delegate → 平台强制让主 agent 试答"
    )
    return r.answer.strip() if r.can_answer else ""


def _outcome_note(outcome: Any) -> str:
    """把判定给出的**进度 / 缺失项**渲染成附注 —— 所有"没完成"的出口都要带上。

    用户要求：*"尽可能暴露一些有用信息到外部"*。
    主 agent 看到"做到什么程度、还缺什么"，才能决定是**换个说法重派**还是**向用户说明**。
    """
    parts: list[str] = []
    if outcome.progress:
        parts.append(f"【已做到】{outcome.progress}")
    if outcome.missing:
        parts.append("【还缺】\n" + "\n".join(f"- {x}" for x in outcome.missing))
    return ("\n\n" + "\n".join(parts)) if parts else ""


def _cmd(runtime: ToolRuntime, recs: list[dict] | None, msg: str) -> Command:
    """返回**这几条** Task —— 合并交给 state 的 reducer。

    并行调用时各写各的，reducer 按 id 合并，不会互相覆盖。
    """
    update: dict[str, Any] = {
        "messages": [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)]
    }
    if recs:
        update["tasks"] = recs
    return Command(update=update)


def _supersede_old(runtime: ToolRuntime, skill_name: str) -> list[dict]:
    """把**同 skill 下已经死掉的**旧 Task 标记为 `superseded`。

    ⚠️ **只取代 `failed` 的。`waiting_for_input` 一律不碰** —— 这是 2026-10-29 改的。

    原来连 `waiting_for_input` 一起取代，理由是"旧待答挂在台账里，
    主 agent 会以为还有事没做完"（S19）。但那个理由是**为了美观**，
    代价却是**静默抹掉一个用户从没回答过的问题**：

    > 实测（F7）：执行器正确地问了"难度是哪个"，平台试答也正确地说"答不上"，
    > 平台告诉主 agent"就向用户提问" —— 结果主 agent 自己编了个"难度：基础"
    > 重新委派，`_supersede_old` 顺手把待答标成 superseded，
    > 于是 `finalize` 看到"没有待答"，什么都不做。**用户从没被问过。**

    现在待答由平台负责投递和透传（`graph._feed_waiting` / `node_finalize`），
    **重派不再是解除待答的路径** —— 所以这里碰它没有意义，只会把问题弄丢。

    - `completed` 的是**历史记录**，不能被抹掉（用户可能还要看产物）
    - `running` 的不会出现在旧 state 里（并行调用各读各的旧快照）

    返回的是**完整的 Task 字典**（不是 patch）—— reducer 按 id 整体覆盖。
    """
    out: list[dict] = []
    for t in runtime.state.get("tasks") or []:
        if t.get("skill") == skill_name and t.get("stage") == "failed":
            out.append({**t, "stage": "superseded"})
    return out


# ──────────────────────────── 重入交接书 ────────────────────────────


def render_briefing(task: dict) -> str:
    """把 Task 渲染成**执行器的输入** —— 所有重入路径的唯一入口。

    🔴 为什么要有它（2026-10-29，F15）：执行器**用完即弃**，每次投递都是一次全新的
    `run_task`，所以"这件事到目前为止的全部已知"必须由平台**重新交给它**。
    原来三条路径（首跑 / 试答后重跑 / 答复投递）各拼各的，**用户历次答复被漏掉** ——
    用户一次答一个时，答第二个就把第一个冲掉了，于是同一个问题问两遍。

    ⚠️ **Task 的每个字段要么渲染在这里、要么在 `evals/unit_briefing.py` 的排除名单里**
    —— 那里有断言，以后加字段忘了渲染会红。

    ⚠️ "你上次说到哪"那块是**上一轮**的情况，措辞要写死这一点：判定也会看到这段，
    别让它把上一轮的"还缺"当成这一轮还没做完。
    """
    from proto.artifacts import ARTIFACTS

    lines = [f"【任务】\n{task.get('task', '')}"]

    progress = (task.get("progress") or "").strip()
    missing = [m for m in (task.get("missing") or []) if m]
    report = (task.get("report") or "").strip()
    if progress or missing or report:
        block = ["【你上次说到哪】（**这是上一轮的情况，不是本轮**）"]
        if progress:
            block.append(f"已做到：{progress}")
        if missing:
            block.append("还缺：\n" + "\n".join(f"- {m}" for m in missing))
        if report:
            block.append(f"你上次说的话：\n{report}")
        lines.append("\n".join(block))

    answers = [a for a in (task.get("answers") or []) if a]
    agent_answer = (task.get("agent_answer") or "").strip()
    if answers or agent_answer:
        block = ["【已经答复过的信息】（按先后顺序）"]
        block += [f"- 用户：{a}" for a in answers]
        if agent_answer:
            block.append(f"- 主 agent 补充：{agent_answer}")
        if len(answers) > 1:
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
            "【已经产出的产物】\n"
            + "\n".join(produced)
            + "\n（这次要**在它基础上补全**，提交完整版；不要从零重写。）"
        )

    refs = _index([a for a in (task.get("refs") or []) if a])
    if refs:
        lines.append(
            "【可读产物】\n"
            + "\n".join(refs)
            + "\n（这是清单，不是正文。要用哪一份就用 read_artifact 读全文，不要凭标题猜。）"
        )

    if progress or missing or report or answers or agent_answer:
        # 只有**重入**才说这句：首跑说"接着干"会让它以为有前情
        lines.append("【续跑提示】\n接着已有的工作继续，**不要重复已完成的工作**。")
    return "\n\n".join(lines)


def _files_note(aids: list[str]) -> str:
    """产物**落在哪个文件** —— 用户问"文件在哪"时，主 agent 得答得上来。

    🔴 **为什么由平台顺路带**：路径是 `write_artifact` 的返回里就有的，但那个返回
    回到的是**执行器**（隔离上下文），主 agent 看不到。主 agent 从 `delegate` 拿到的
    只有一句 `已交付 art_001。摘要：…` —— 于是它在回复里**只能报 ID、报不出路径**：
    **不是它不想说，是它不知道**（用户 2026-10-30：*"你为啥就是不给我显示产物路径呢"*）。

    用户给的解法就是这个：*"这个 tool 返回的时候顺便把产物路径返回不就行了吗"* ——
    **信息本来就在，顺着原路带上去就行**，不需要另起一套平台注入。

    ⚠️ 没设 `ARTIFACT_DIR` 时（跑批）产物最后才统一落盘，这时没有 `path` —— 那就**不带**
    （不编一个假的）。
    """
    from proto.artifacts import ARTIFACTS

    rows = [
        f"{aid} → {ARTIFACTS[aid]['path']}"
        for aid in (aids or [])
        if aid in ARTIFACTS and ARTIFACTS[aid].get("path")
    ]
    return "\n文件：" + "\n".join(rows) if rows else ""


def absorb_outcome(rec: dict, outcome: Any) -> None:
    """把执行器的结果**写进 Task** —— 四个字段一个都不能少（F15 的同类：存了才可能喂）。

    ⚠️ `artifacts` 只在非空时覆盖：执行器可能这一轮没产出，不能把上一轮的抹掉。
    """
    rec["report"] = outcome.report
    rec["progress"] = outcome.progress
    rec["missing"] = list(outcome.missing)
    if outcome.artifacts:
        rec["artifacts"] = list(outcome.artifacts)


# ──────────────────────────── 委派 ────────────────────────────


@tool
def delegate(
    skill_name: str,
    task: str,
    runtime: ToolRuntime,
    artifacts: list[str] | None = None,
) -> Command:
    """把**一件具体工作**委派给隔离的执行器（它在独立上下文里干，只把产物和摘要带回来）。

    **需要专业能力时一律用它**；闲聊、追问、澄清自己直接答。
    执行器在独立上下文里干活，产出不会撑爆你的上下文。

    Args:
        skill_name: 能力名。
        task: 这件工作的具体要求。**不要把前序产物的正文抄进来** —— 需要它作为输入时，
            把产物 ID 放进 `artifacts`，执行器会自己读全文（转述既失真、又白占你的上下文）。
        artifacts: 这次工作要参考的产物 ID（如 `["art_001"]`）。不确定有哪些产物先
            `list_artifacts()`；不确定某个能力交付什么先 `read_skill(name)`。
    """
    skill = SKILLS.get(skill_name)
    if skill is None:
        return _cmd(runtime, None, f"没有能力「{skill_name}」。可用：{'、'.join(SKILLS)}")

    # 产物引用**当场校验**：编出来的 ID 直接拒掉，并给出真实清单 ——
    # 放行的话，执行器读不到会自己找补，错误被埋进产物里。
    refs = [a for a in (artifacts or []) if a]
    unknown = [a for a in refs if a not in ARTIFACTS]
    if unknown:
        have = "；".join(artifact_index_lines()) or "（本会话还没有产物）"
        return _cmd(
            runtime, None,
            f"产物 {'、'.join(unknown)} 不存在，**这次没有派发**。本会话现有产物：{have}",
        )

    # id 用 tool_call_id 派生 —— **并行调用时不会撞车**（用序号会撞，实测踩过）。
    # ⚠️ 必须取**尾部**：tool_call_id 形如 `call_abc123...`，前几位所有调用都一样。
    tid = f"t{(runtime.tool_call_id or 'x')[-8:]}"
    # 同 skill 下还没了结的旧 Task → 标记 superseded，免得台账里挂着过期项
    superseded = _supersede_old(runtime, skill_name)
    rec: dict[str, Any] = {
        "id": tid, "skill": skill_name, "task": task,
        "stage": "running", "artifacts": [], "report": "", "agent_tried": False,
        # 判定的产出也要进 Task 记录 —— 台账注入时主 agent 才看得到
        "progress": "", "missing": [],
        # ⚠️ `artifacts` 是**产出**（产物 ID 列表），`refs` 是**输入引用** —— 两个方向，别混。
        # 待答投递（`graph._feed_waiting`）要按 refs 原样重建可读产物清单。
        "refs": refs,
        # 历次答复与主 agent 补的信息都要**累积在 Task 上**：执行器用完即弃，
        # 不累积的话下一次投递会把之前的答复冲掉（2026-10-29 人在环实测踩过）。
        "answers": [],
        "agent_answer": "",
    }
    trace.section(f"delegate · {skill_name} · {tid}")
    if superseded:
        trace.note(f"取代了 {len(superseded)} 个同技能的未了结 Task：{[t['id'] for t in superseded]}")

    def _out(rec: dict, msg: str) -> Command:
        """统一的返回口 —— **总是带上被取代的旧 Task**。"""
        return _cmd(runtime, [rec, *superseded], msg)

    # ① 派发 —— run_task 内部已含「出口限制」：既没完成也没提问会被推回去继续
    outcome = run_task(skill, render_briefing(rec))
    absorb_outcome(rec, outcome)

    # ② 按**三态枚举**分派 —— 穷举，没有第四种（用户「封闭原则」）
    if outcome.status == "completed":
        rec.update(stage="completed", artifacts=outcome.artifacts)
        if outcome.artifacts:
            return _out(rec,
                        f"已交付 {', '.join(outcome.artifacts)}"
                        f"{_files_note(outcome.artifacts)}。摘要：{outcome.report[:150]}")
        return _out(
            rec,
            "已交付（成果在执行器的回复里，**请原样转达给用户，不要改写**——"
            f"它可能是调用方要解析的协议块）：\n\n{outcome.report}",
        )

    if outcome.status == "waiting_for_input":
        # ③ 在请求信息 → 平台强制先让主 agent 试答
        answer = _ask_main(runtime, skill_name, task, outcome)
        if answer:
            rec["agent_tried"] = True
            # 记在 Task 上：这次补的信息在后续投递（用户答复）里也要带上，
            # 否则执行器换一次上下文就把它忘了。
            rec["agent_answer"] = answer
            # 试答后重跑也是**重入**：走同一份交接书（带上刚才那一轮的进度 / 它问过什么）
            o2 = run_task(skill, render_briefing(rec))
            absorb_outcome(rec, o2)
            if o2.status == "completed":
                rec.update(stage="completed", artifacts=o2.artifacts, report=o2.report)
                return _out(
                    rec,
                    f"已交付 {', '.join(o2.artifacts) or '（成果在回复里）'}"
                    f"{_files_note(o2.artifacts)}"
                    f"（你补了信息：{answer[:60]}）。摘要：{o2.report[:150]}",
                )
            outcome = o2  # 补了还是不行 → 用新的结果去问用户（三态可能变成 incomplete）

        if outcome.status == "completed":
            rec.update(stage="completed", artifacts=outcome.artifacts, report=outcome.report)
            return _out(
                rec,
                f"已交付 {', '.join(outcome.artifacts) or '（成果在回复里）'}"
                f"{_files_note(outcome.artifacts)}。",
            )

        if outcome.status == "waiting_for_input":
            rec.update(stage="waiting_for_input", report=outcome.report,
                       agent_tried=rec["agent_tried"],
                       progress=outcome.progress, missing=list(outcome.missing))
            trace.note(f"delegate · {tid} 登记待答（平台负责转达，不由主 agent 代问）")
            return _out(
                rec,
                f"⚠️ 这次没完成，它在请求信息。执行器说：{outcome.report}"
                f"{_outcome_note(outcome)}\n"
                "（平台已登记为待答，**并会负责把这个问题转达给用户**，"
                "用户的答复也会由平台投回给它 —— 你不用重复问，"
                "更不要自己替用户定一个答案。你这条回复说清楚「派了什么、还差什么」就行。）",
            )
        # 补了信息之后反而变成 incomplete → 落到下面的 failed 分支

    # ④ 只剩 `incomplete` —— 推了 N 轮仍既没完成、也没请求信息，执行器是真的干不了。
    #    按评审人 E2「情况 A：执行失败」→ **交给主 agent 决定**，而不是拿去烦用户。
    #    ⚠️ 附注必须带上 —— 主 agent 要知道"做到什么程度、还缺什么"才能决定下一步。
    rec.update(stage="failed", report=outcome.report,
               progress=outcome.progress, missing=list(outcome.missing))
    trace.note(f"delegate · {tid} status=incomplete（推了 {outcome.rounds} 轮）→ failed")
    return _out(
        rec,
        f"⚠️ 执行器跑了 {outcome.rounds} 轮，仍然既没完成任务、也没说明需要什么信息。"
        f"{_outcome_note(outcome)}\n\n"
        f"它最后说：{outcome.report[:300]}\n"
        "（你可以换个说法再 delegate 一次；或者向用户说明这次没做成。）",
    )


# ──────────────────────────── 装配 ────────────────────────────


def build_agent(state_schema: type) -> Any:
    """主 agent：一个完整的 agent（工具 + 循环）。"""
    return create_agent(
        get_model(),
        tools=[delegate, write_artifact, read_artifact, list_artifacts, read_skill,
           read_reference, list_references_tool],
        system_prompt=_MAIN_PROMPT + "\n\n可用能力：\n" + "\n".join(
            f"- {s.name}：{s.description}" for s in SKILLS.values()
        ),
        state_schema=state_schema,
        name="main_agent",
    )
