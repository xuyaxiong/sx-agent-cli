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
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from proto import trace
from proto.config import get_model
from proto.executor import run_task
from proto.handoff import SECTION  # 交接的**段名**（唯一来源）
from proto.handoff.ask import build_prompt as ask_prompt  # 试答
from proto.handoff.briefing import absorb_outcome, render_briefing  # 委托单
from proto.handoff.receipt import build_receipt, build_rejection, render_receipt  # 回执
from proto.llm import safe_structured
from proto.artifacts import (  # 产物领域
    ARTIFACTS,
    artifact_index_lines,
    list_artifacts,
    read_artifact,
    write_artifact,
)
from proto.skill_pack import read_skill_file  # 技能包文件（磁盘）
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
    "## 🔴 用户原话由平台**原样转交**，你不用复述\n"
    "\n"
    "你派发时**不用**复述用户说过的话，也**不用**标注「这条需求出自哪一轮」——\n"
    "平台会把**每一轮用户原话**都放进执行器手里的索引（序号 · 字数 · 开头 · 它在回答什么），\n"
    "需要哪一轮的全文，执行器自己按需读。**比你转述可靠**：你写的是理解，原文是事实。\n"
    "\n"
    "所以 `task` 只写「这次要什么」—— 不要写「内容是什么」。\n"
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
    # 🔴 **不截断**（用户 2026-11-02 的「交接三问」之二）：这一步是个**独立的结构化调用** ——
    # 它**只看得见这个提示词里的东西**。信息若落在长消息的后半段，它就答不上来、
    # 只能去打扰用户 —— 而"能不打扰用户就不打扰"正是这一步存在的全部意义。
    # 主 agent 的上下文本来就小（隔离生效），整份给**全文**是便宜的。
    # ⚠️ **别退回"最近 N 条"**：这一步问的就是"**你的上下文里**有没有"，
    # 砍掉前文等于让它答不上来 —— 那是个**无声**的范围限制，比截断更难发现。
    msgs = runtime.state.get("messages") or []
    prompt = ask_prompt(skill_name, task, outcome, msgs)
    # 交接关键日志：这一步把**主 agent 的全部上下文**又发了一遍（判据③：膨胀代价看得见）
    trace.handoff("试答", "平台", "主 agent", prompt,
                  structured=f"ExecOutcome ＋ 全部 {len(msgs)} 条消息（逐字见下）")
    # 调用失败时保守当作「答不上」—— 交给用户，而不是编一个答案
    r = safe_structured(
        _AgentAnswer, prompt, _AgentAnswer(can_answer=False),
        tag="delegate → 平台强制让主 agent 试答",
        kind="ask_main",
        # ⚠️ 返回**不原样再记一遍**：下面那条决策记录里已经有排好版的「结果」——
        # 再来一条 `can_answer=True answer='课型：…\n学生基础：…'`（转义 repr）
        # 就是同一件事两副面孔（用户 2026-11-03：**不要搞两套**）。
        log_response=False,
    )
    # 🔴 **一次决策 = 一条记录**（用户 2026-11-03）：前提 → 问题 → 结果 → 下一步。
    # 前提的**全文**就是上面那条 PROMPT（主 agent 的整个上下文，不截）——
    # 这里只说清"它看的是什么"，不重复贴一遍。
    ans = r.answer.strip()
    trace.decision(
        "试答（LLM）· 平台强制的一步（在 delegate 内部）",
        q="**主 agent 的上下文里**，有没有执行器要的那个信息？（有就不打扰用户）",
        given=[
            f"执行器问的那句（原话，{len(outcome.report)} 字）",
            "判定说的「已做到 / 还缺」（第三方评审，可能判错）",
            f"主 agent 的全部 {len(msgs)} 条消息 —— **全文、不截**，逐字见上面那条 PROMPT",
        ],
        result=[
            f"can_answer = **{r.can_answer}**",
            f"补充：{ans or '（无）'}",
        ],
        nxt=(
            "成对记进 `Task.qa`（`by=主 agent`）→ 带回去重跑执行器，**不打扰用户**"
            if r.can_answer
            else "答不上 → 登记待答，由**平台**把执行器原话**透传**给用户"
        ),
    )
    return ans if r.can_answer else ""


def _cmd(
    runtime: ToolRuntime, recs: list[dict] | None, msg: str, *, handoff: dict | None = None
) -> Command:
    """返回**这几条** Task —— 合并交给 state 的 reducer。

    并行调用时各写各的，reducer 按 id 合并，不会互相覆盖。

    🔴 `handoff` = **回执对象**，挂在 ToolMessage 的 `additional_kwargs` 上**搭同一趟车**：
    模型只看 `content`（渲染出来的文本），**记录/审计层读这个对象** ——
    实测 `additional_kwargs` **不会漏给模型**（`_convert_message_to_dict` 只取
    content / role / tool_call_id）。这样记录层永远不需要**从文本反推结构**
    （用户 2026-11-03 定的硬规矩，见 `proto/handoff/receipt.py` 的 docstring）。
    """
    update: dict[str, Any] = {
        "messages": [
            ToolMessage(
                content=msg,
                tool_call_id=runtime.tool_call_id,
                additional_kwargs=({"handoff": handoff} if handoff else {}),
            )
        ]
    }
    if recs:
        update["tasks"] = recs
    return Command(update=update)


def _reject(runtime: ToolRuntime, kind: str, detail: str, available: str) -> Command:
    """**派发被拒**（不建 Task）→ 回执对象 ＋ 渲染 —— 和正常回执走**同一条**路。"""
    r = build_rejection(kind, detail, available)
    return _cmd(runtime, None, render_receipt(r), handoff=r)


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


# ──────────────────────────── 用户原话（平台硬搬） ────────────────────────────


def _user_turns(messages: list) -> list[dict]:
    """把对话历史里的**用户原话**抽出来，按顺序编号 —— 平台硬搬，不经任何转述。

    🔴 **为什么要搬**（2026-11-02）：首次派发时执行器看到的"任务"**100% 是主 agent 的转述**，
    而转述**不只丢、还会加** —— 实测一次真实运行里，主 agent 自己补了用户没说的
    「人教版三年级下册」「叶圣陶的经典散文」（`docs/solution.md` §9.2 有完整对照）。
    丢了能靠追问补回来；**加了会变成一份"基于错误前提的成品"，而用户不知道**。

    ⚠️ **只认 `HumanMessage`**：主图里注入的台账是 `SystemMessage`、透传是 `AIMessage`，
    所以"HumanMessage = 用户本人说的"在**主图**里成立（执行器自己的图里不成立，别复用）。

    ⚠️ 这里存的是**全文**（可能很长，用户会粘长文）—— 交接书里只渲染**开头几个字**，
    全文由执行器按需用 `read_user_turn(n)` 取（渐进式披露，见 `render_briefing` 那段）。

    🔴 **每轮还要带上"它在回答的那一句"（`after`）**（用户 2026-11-02）：

    > "交接时上下文一定要清楚，**一问一答**。"

    实测（`interactive-skills-1102-174302`）：主 agent 问「学生填写形式你偏好哪一种？
    A. 填空补白 / B. 表格记录 / C. 图示思维导图 / D. 混合」，用户答「A」——
    而交接书里只有一行 `第 4 轮（1 字）：A`，**选项列表不在里面**。
    这一段的全部意义是**对冲转述失真**：主 agent 要是把 A 写成"表格记录"，
    执行器**发现不了**（它手里那个「A」既解读不了、也核对不了）。
    更糟的是这段还写着"冲突时以原话为准" —— 等于把一个**无法解读的片段**标成了最高权威。

    `after` 取**紧邻的前一条 AI 消息**（纯位置关系，平台搬，不判断语义）。
    它可能是主 agent 说的，也可能是**平台把执行器自己的提问透传给用户的那条**
    （`node_finalize` 注入的）—— 两种情况执行器都需要，所以不区分。
    """
    out: list[dict] = []
    last_ai = ""
    for m in messages:
        if isinstance(m, AIMessage):
            text = str(m.content).strip()
            if text:
                last_ai = text
            continue
        if not isinstance(m, HumanMessage):
            continue
        text = str(m.content).strip()
        if text:
            out.append({"n": len(out) + 1, "text": text, "after": last_ai})
            # 一个"上文"只配一轮：连着两条用户消息时，第二条的语境是第一条，
            # 不该再挂同一条 AI 消息（挂了就是**错的**上下文，比没有更糟）。
            last_ai = ""
    return out


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

    ⚠️ **用户原话由平台原样转交**：每一轮都在执行器手里的索引里（序号 · 字数 · 开头 ·
    它在回答什么），需要哪一轮的全文它自己按需读。所以你**不用**在 `task` 里复述用户的话，
    也不用标注依据哪一轮。

    Args:
        skill_name: 能力名。
        task: 这件工作的具体要求。**不要把前序产物的正文抄进来** —— 需要它作为输入时，
            把产物 ID 放进 `artifacts`，执行器会自己读全文（转述既失真、又白占你的上下文）。
        artifacts: 这次工作要参考的产物 ID（如 `["art_001"]`）。不确定有哪些产物先
            `list_artifacts()`；不确定某个能力交付什么先 `read_skill(name)`。
    """
    skill = SKILLS.get(skill_name)
    if skill is None:
        return _reject(runtime, "rejected_skill", f"❌ 没有能力「{skill_name}」", "、".join(SKILLS))

    # 用户原话**平台硬搬**（快照进 Task）—— 见 `_user_turns` 的 docstring。
    # ⚠️ 平台**不挑**哪几轮给全文（原来由主 agent 用 `based_on_turns` 点，见 `turn_lines`
    # 的 docstring）：全给索引、全文按需取。
    turns = _user_turns(runtime.state.get("messages") or [])

    # 产物引用**当场校验**：编出来的 ID 直接拒掉，并给出真实清单 ——
    # 放行的话，执行器读不到会自己找补，错误被埋进产物里。
    refs = [a for a in (artifacts or []) if a]
    unknown = [a for a in refs if a not in ARTIFACTS]
    if unknown:
        have = "；".join(artifact_index_lines()) or "（本会话还没有产物）"
        return _reject(
            runtime, "rejected_artifact",
            f"❌ 产物 {'、'.join(unknown)} 不存在", have,
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
        # 🔴 **历次问答对，累积在 Task 上**（用户 2026-11-02 定的形状）：
        # `[{"by": "用户"|"主 agent", "q": 执行器问的原话, "a": 答复}]`
        #
        # **为什么是"对"而不是两个平行列表**：执行器用完即弃，每次重入都要重放全部已知；
        # 而 `report`（它问的那句）是**覆盖**写的 —— 答案累积、问题覆盖的话，
        # 第二次重入之后旧答案就没有指涉了（"答第二个把第一个冲掉"的镜像版）。
        #
        # **为什么不分两个字段**（原来是 `answers` ＋ `agent_answer`）：那是**按来源分叉**，
        # 而"谁答的"本来就是每条记录的一个属性 —— 一个列表 ＋ 一个 `by` 字段就够了。
        #
        # **问题由平台原样截下，不问 LLM**：用户答的那条在 `_feed_waiting` 截
        # （那一刻 `report` 就是它问的），主 agent 试答那条在 `delegate` 里截。
        "qa": [],
        # 用户原话**快照**（不是渲染交接书时现取），两个理由：
        # ① 答复投递那条路（`graph._feed_waiting`）也调 `render_briefing`，快照在 Task 上它自动跟着走
        # ② 与 `answers` **天然去重** —— 快照是"派发**之前**用户说的"，
        #    `answers` 是"派发**之后**用户答的"，时间上不重叠
        "user_turns": turns,
    }
    # kind：语义标签，展示层（订阅事件的那层）按它归角色 —— 不靠子串认行
    trace.section(f"delegate · {skill_name} · {tid}", kind="dispatch")
    if superseded:
        trace.note(f"取代了 {len(superseded)} 个同技能的未了结 Task：{[t['id'] for t in superseded]}",
                   kind="supersede")

    def _out(rec: dict, outcome: Any, *, agent_answer: str = "", note: str = "") -> Command:
        """统一的返回口 —— **总是带上被取代的旧 Task**。

        🔴 它做两件事，**一次做完**：构造**回执对象**、渲染成文本 ——
        文本进 `content`（模型只看这个），对象挂 `additional_kwargs`（模型看不到，
        记录层读它）。见 `proto/handoff/receipt.py` 的 docstring。
        """
        r = build_receipt(outcome, agent_answer=agent_answer, note=note)
        text = render_receipt(r)
        # 交接关键日志（复盘用）：这一跳交了多少、有没有结构化对象 —— 见 `trace.handoff`。
        # 正文**不在这里 dump**：主 agent 的逐步骤 dump 里已经有它（`graph.node_agent`）。
        trace.handoff("回执", "执行器", "主 agent", text,
                      structured=f"Receipt(kind={r['kind']}，{len(r['sections'])} 段)")
        return _cmd(runtime, [rec, *superseded], text, handoff=r)

    # ① 派发 —— run_task 内部已含「出口限制」：既没完成也没提问会被推回去继续
    # 第三个参数是用户原话（给执行器的 `read_user_turn` 用）—— 交接书里只有清单，全文按需取
    outcome = run_task(skill, render_briefing(rec), rec.get("user_turns"))
    absorb_outcome(rec, outcome)

    # ② 按**三态枚举**分派 —— 穷举，没有第四种（用户「封闭原则」）
    if outcome.status == "completed":
        rec.update(stage="completed", artifacts=outcome.artifacts)
        if outcome.artifacts:
            return _out(rec, outcome)
        return _out(
            rec, outcome,
            note="成果在**执行器的回复里**（上面【执行器说】那段）——"
                 "**请原样转达给用户，不要改写**：它可能是调用方要解析的协议块。",
        )

    if outcome.status == "waiting_for_input":
        # ③ 在请求信息 → 平台强制先让主 agent 试答
        answer = _ask_main(runtime, skill_name, task, outcome)
        if answer:
            rec["agent_tried"] = True
            # 🔴 **成对记**（`by="主 agent"`）：只记答案的话，下一次重入时
            # `report` 已经被新问题覆盖，这条答案就没了指涉 —— 同 `qa` 那段注释。
            # 问题**就是这一刻的 `outcome.report`**，平台原样截下，不问 LLM。
            rec["qa"] = [*(rec.get("qa") or []),
                         {"by": "主 agent", "q": outcome.report, "a": answer}]
            # 试答后重跑也是**重入**：走同一份交接书（带上刚才那一轮的进度 / 它问过什么）
            o2 = run_task(skill, render_briefing(rec), rec.get("user_turns"))
            absorb_outcome(rec, o2)
            if o2.status == "completed":
                rec.update(stage="completed", artifacts=o2.artifacts, report=o2.report)
                return _out(rec, o2, agent_answer=answer)
            outcome = o2  # 补了还是不行 → 用新的结果去问用户（三态可能变成 incomplete）

        if outcome.status == "completed":
            rec.update(stage="completed", artifacts=outcome.artifacts, report=outcome.report)
            return _out(rec, outcome, agent_answer=answer)

        if outcome.status == "waiting_for_input":
            rec.update(stage="waiting_for_input", report=outcome.report,
                       agent_tried=rec["agent_tried"],
                       progress=outcome.progress, missing=list(outcome.missing))
            trace.note(f"delegate · {tid} 登记待答（平台负责转达，不由主 agent 代问）",
                       kind="waiting")
            return _out(
                rec, outcome,
                note="平台已登记为待答，**并会负责把这个问题转达给用户**，"
                     "用户的答复也会由平台投回给它 —— 你**不用**重复问，"
                     "更不要自己替用户定一个答案。你这条回复说清楚「派了什么、还差什么」就行。",
            )
        # 补了信息之后反而变成 incomplete → 落到下面的 failed 分支

    # ④ 只剩 `incomplete` —— 推了 N 轮仍既没完成、也没请求信息，执行器是真的干不了。
    #    按评审人 E2「情况 A：执行失败」→ **交给主 agent 决定**，而不是拿去烦用户。
    #    ⚠️ 附注必须带上 —— 主 agent 要知道"做到什么程度、还缺什么"才能决定下一步。
    rec.update(stage="failed", report=outcome.report,
               progress=outcome.progress, missing=list(outcome.missing))
    trace.note(f"delegate · {tid} status=incomplete（推了 {outcome.rounds} 轮）→ failed",
               kind="delegate_failed")
    return _out(
        rec, outcome,
        note="你可以**换个说法再 delegate 一次**；或者向用户说明这次没做成。",
    )


# ──────────────────────────── 装配 ────────────────────────────

#: **执行路径开关** —— 主 agent 到底"自己干"还是"派出去"。
#:
#:   - `"auto"`（**默认，产品行为**）：两条路都开，由它自己选
#:   - `"delegate"` **强制委派**：裁掉"自己干"的工具（`write_artifact` /
#:     `read_skill` / `read_skill_file`）→ 它**拿不到原料**，只能派出去
#:   - `"inline"` **强制主 agent 自己干**：**不给 `delegate`** → 工具不存在，它调不了
#:
#: 🔴 **为什么要这个开关**（用户 2026-11-02）：跑批时想验某条链路，只能**碰运气**
#: 碰上一个模型愿意走那条路的 case —— 而"走哪条路"是**模型的自由选择**，
#: 于是那条断言必然脆（判据见 `evals/checks.py` 顶部）。
#: 有了开关，**测哪条就开哪条**，不用再"妄图正好有一条符合路径的 case"。
#:
#: ⚠️ **两档的硬度不一样**，别当成一回事：
#:   - `"inline"` 是**全硬**的 —— `delegate` 工具根本不存在，它调不了。
#:   - `"delegate"` 是**半硬**的 —— 裁掉工具能拿掉它的原料（读不到技能、写不了产物），
#:     但**挡不住它在回复里直接写**。所以这一档还要配一句系统提示（`_MODE_NOTE`），
#:     并且跑批时用 `task_count_at_least` 盯着"到底派没派"。
EXEC_PATH = "auto"

#: 三种模式各给哪些工具。⚠️ 改这里时想清楚**拿掉某个工具会不会顺手拿掉一条合法能力**。
_TOOLS: dict[str, list] = {
    "auto": [delegate, write_artifact, read_artifact, list_artifacts, read_skill, read_skill_file],
    "delegate": [delegate, list_artifacts],
    "inline": [write_artifact, read_artifact, list_artifacts, read_skill, read_skill_file],
}

#: 非默认模式下，往系统提示里补一句 —— 工具集变了，得让它知道**为什么**。
_MODE_NOTE: dict[str, str] = {
    "delegate": (
        "\n\n🔴 **本轮强制委派**：专业能力的活**一律**用 `delegate` 派出去，"
        "**不要自己在回复里写成品** —— 你手边既没有产出工具，也没有技能原文。"
    ),
    "inline": (
        "\n\n🔴 **本轮不委派**：你手边**没有 `delegate` 工具**，"
        "专业能力的活自己按能力清单和技能原文做。"
    ),
}


def build_agent(state_schema: type) -> Any:
    """主 agent：一个完整的 agent（工具 + 循环）。

    ⚠️ **工具集由 `EXEC_PATH` 决定**（默认 `"auto"` = 两条路都开）。
    """
    return create_agent(
        get_model(),
        tools=list(_TOOLS[EXEC_PATH]),
        system_prompt=_MAIN_PROMPT
        + _MODE_NOTE.get(EXEC_PATH, "")
        + "\n\n可用能力：\n"
        + "\n".join(f"- {s.name}：{s.description}" for s in SKILLS.values()),
        state_schema=state_schema,
        name="main_agent",
    )
