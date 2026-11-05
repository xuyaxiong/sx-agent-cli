"""主 agent —— **一个完整的 agent**（有自己的工具与循环），不是纯路由器。

三种出路（**由它自己决定**）：
  1. 直接答              —— 闲聊、追问、澄清（**不建 Task**）
  2. 自己产出短内容       —— write_artifact（**不建 Task**）
  3. `delegate` 委派      —— 需要专业技能时（**建 Task**）

**`delegate` 内部由平台保证四件事**（LLM 跳不过）：

    ⓪ **取代旧的**：同 skill 下未了结的旧 Task → 标记 `superseded`（免得台账挂过期项）
    ① **执行器干活**（`run_task`，隔离上下文；内部含"出口限制"，最多 react 3 轮）
    ② **先让主 agent 试答**：用它自己的完整上下文 + 进度 + 缺失项；答得上就不打扰用户
    ③ **登记待答**：答不上才 `stage = waiting_for_input`，交给主 agent 去问用户

**Task 只在决定委派时才建。** 不委派就没有 Task。
"""

from __future__ import annotations

from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from proto import trace
from proto.config import get_model
from proto import executor  # ⚠️ 走模块属性，见 delegate 的注释
from proto.handoff.ask import build_prompt as ask_prompt  # 试答
from proto.handoff.briefing import absorb_outcome, render_briefing, settle  # 委托单
from proto.handoff.receipt import build_receipt, build_rejection, render_receipt  # 回执
from proto.llm import safe_structured
from proto import artifacts as artifacts_mod  # 产物领域（唯一入口是它的读 API）
from proto.artifacts import (
    artifact_index_lines,
    list_artifacts,
    read_artifact,
    write_artifact,
)
from proto.skill_pack import read_skill_file  # 技能包文件（磁盘）
from proto.skills import read_skill  # 技能领域
from proto.registry import registry  # 技能注册表（唯一持有者）

#: 主 agent 的 system prompt —— 平台约定，**不约束 skill 作者**
_MAIN_PROMPT = (
    "你是一个能自己干活的助手，**不必把所有事都委派出去**。\n"
    "\n"
    "两种做法：\n"
    "1. **直接答**：闲聊、追问、解释、澄清、简单修改——不需要专业技能的事，你自己说。\n"
    "2. **delegate 委派**：需要下面列出的**专业技能**时，**一律委派**，不要自己凭感觉做。\n"
    "   执行器在独立上下文里按该技能的指令干活，只把产物和摘要带回来。\n"
    "\n"
    "为什么必须委派：专业技能的产出可能很长，自己干会把你的上下文撑爆。\n"
    "**你在派发前无法判断产出有多长**，所以不要猜——需要专业技能就委派。\n"
    "\n"
    "委派回来如果显示「它在**要信息**」——**什么都别做，尤其不要自己补一个答案**：\n"
    "平台已经替你查过一次上下文了（`delegate` 内部做的）；确实没有，\n"
    "平台会把**它申报的那句原话**贴给用户，用户答复后也会**投回给那个执行器**。\n"
    "🔴 **你这条回复只说「派了什么」就行 —— 问题原文由平台贴，你不要写。**\n"
    "（问题内容在台账那行「**它要的信息**」里，你看得到；看得到是为了**别重复问**，\n"
    "不是让你转述 —— 转述会和平台那条**重复**，用户会看到同一个问题两遍。）\n"
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
    "- 不确定某个技能**交付什么、按什么标准交付** → `read_skill(name)` 读原文核对，\n"
    "  **不要凭一句话描述猜**：你猜出来的要求会压过技能自己的交付标准。\n"
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


def _ask_main(messages: list, skill_name: str, task: str, outcome: Any) -> str:
    """**平台强制的一步**：让主 agent 用自己的上下文试着补上缺的信息。

    ⚠️ **必须把"它要什么"给全**（信息交接原则）：现在那是**执行器自己申报的原话**
    （`ask_user` 参数 ✓ 结构 ✓）—— 不用从它说的话里挑哪句是问题。
    （⚠️ 2026-11-05 判定退休：原来这里要求带"判定说的已做到 / 还缺什么"—— 那两样
    是判定吐的，判定没了就没有来源了。）

    ⚠️ 入参是**消息列表**而不是 `ToolRuntime`（2026-11-04 抽 `try_answer_then_rerun` 时改）：
    这一步**两条路都要用** —— `delegate` 有 runtime、`graph._feed_waiting` 只有 state，
    而这里真正要的只是 `messages`（原来从 runtime 取的也只有这一个东西）。
    """
    # 🔴 **不截断**（用户 2026-11-02 的「交接三问」之二）：这一步是个**独立的结构化调用** ——
    # 它**只看得见这个提示词里的东西**。信息若落在长消息的后半段，它就答不上来、
    # 只能去打扰用户 —— 而"能不打扰用户就不打扰"正是这一步存在的全部意义。
    # 主 agent 的上下文本来就小（隔离生效），整份给**全文**是便宜的。
    # ⚠️ **别退回"最近 N 条"**：这一步问的就是"**你的上下文里**有没有"，
    # 砍掉前文等于让它答不上来 —— 那是个**无声**的范围限制，比截断更难发现。
    msgs = messages or []
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
        # ⚠️ 不再写"在 delegate 内部"（2026-11-04 E1）：这一步**两条路都会走**
        # （首次派发 / 投递重跑），写死位置会变成假话。
        "试答（LLM）· 平台强制的一步",
        q="**主 agent 的上下文里**，有没有执行器要的那个信息？（有就不打扰用户）",
        given=[
            # 🔴 **它要什么 = `ask_user` 参数里的原话**（结构 ✓）—— 判定退休后没有
            #    "判定说的已做到/还缺"那两样了（那本来是判定吐的）。
            f"执行器申报要的信息（`ask_user` 参数原话，{len(outcome.asked)} 字）：{outcome.asked}",
            f"它最后说的那段（{len(outcome.report)} 字，可能有上下文、也可能只是过场）",
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


def try_answer_then_rerun(
    rec: dict, skill: Any, outcome: Any, *, messages: list
) -> tuple[Any, str]:
    """执行器说「缺信息」→ **平台强制主 agent 先用自己上下文试答** → 答得上就重跑一遍。

    🔴 **两条路共用**（2026-11-04 用户定 E1：*"肯定得统一啊 都先走试答啊"*）：
    首次派发（`delegate`）和**投递重跑**（`graph._feed_waiting`）走**同一套**处置 ——
    原来投递那条路直接又登记待答：用户刚答完一轮就**又被问一遍**，而主 agent 这时
    **已经知道用户刚说的那句**、很可能答得上。同一个状态两套处理，正是要治的病。

    返回 `(最终 outcome, 主 agent 的试答)`：
      - 试答为空 → 原样返回（调用方去登记待答）
      - 试答非空 → **成对**记进 `qa`（`q` 是它问的那句原话）→ 重跑 → 返回新 outcome

    ⚠️ **只做这一步，不决定出口** —— completed / 仍待答 / failed 由调用方按最终 outcome
    分派（三个出口的措辞各不相同，混进来就又长出一个上帝函数）。
    """
    answer = _ask_main(messages, skill.name, rec.get("task") or "", outcome)
    if not answer:
        return outcome, ""
    rec["agent_tried"] = True
    # 🔴 **成对记**（`by="主 agent"`）：只记答案的话，下一次重入时 `report` 已经被
    # 新问题覆盖，这条答案就没了指涉 —— 同 `qa` 那段注释。
    # 🔴 **「问」取 `outcome.asked`（`ask_user` 的参数原话），不取 `report`**
    #    （2026-11-04 日志定位，同 `graph._feed_waiting` 那一处）：`report` 是它
    #    最后说的**那段话**，混着过场；拿它当「问」，配对就失去指涉。
    rec["qa"] = [*(rec.get("qa") or []),
                 {"by": "主 agent", "q": outcome.asked, "a": answer}]
    # 试答后重跑也是**重入**：走同一份交接书（带上刚才那一轮的进度 / 它问过什么）
    o2 = executor.run_task(skill, render_briefing(rec), rec.get("user_turns"))
    return o2, answer


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

    🔴 **2026-11-04（F18 修）之后这一行的取法变了**：`node_finalize` **不再注入**
    那条 `AIMessage`（它让模型以为问题是自己说的）—— 所以这里取到的**不再是平台透传的
    问题**，而是**主 agent 自己那条回复**（而它现在被要求**别写问题**，可能压根没提）。

    ⇒ **"答不能脱离问"这件事现在靠【问答经过】兜**（`qa[].q` ＝ 收下答案那一刻的
    `Task.asked`，结构 ✓）。这一行降级成**次级线索**。
    ⚠️ **待实测确认够不够** —— 不够的话，正解是让 `_user_turns` 在"这一轮是答复"时
    直接取 `Task.asked`（而不是从 `messages` 里找前一条 AI 消息）。
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

    **需要专业技能时一律用它**；闲聊、追问、澄清自己直接答。
    执行器在独立上下文里干活，产出不会撑爆你的上下文。

    ⚠️ **用户原话由平台原样转交**：每一轮都在执行器手里的索引里（序号 · 字数 · 开头 ·
    它在回答什么），需要哪一轮的全文它自己按需读。所以你**不用**在 `task` 里复述用户的话，
    也不用标注依据哪一轮。

    Args:
        skill_name: 技能名。
        task: 这件工作的具体要求。**不要把前序产物的正文抄进来** —— 需要它作为输入时，
            把产物 ID 放进 `artifacts`，执行器会自己读全文（转述既失真、又白占你的上下文）。
        artifacts: 这次工作要参考的产物 ID（如 `["art_001"]`）。不确定有哪些产物先
            `list_artifacts()`；不确定某个技能交付什么先 `read_skill(name)`。
    """
    skill = registry.get(skill_name)
    if skill is None:
        return _reject(runtime, "rejected_skill", f"❌ 没有技能「{skill_name}」",
                       "、".join(registry.names()))

    # 用户原话**平台硬搬**（快照进 Task）—— 见 `_user_turns` 的 docstring。
    # ⚠️ 平台**不挑**哪几轮给全文（原来由主 agent 用 `based_on_turns` 点，见 `turn_lines`
    # 的 docstring）：全给索引、全文按需取。
    turns = _user_turns(runtime.state.get("messages") or [])

    # 产物引用**当场校验**：编出来的 ID 直接拒掉，并给出真实清单 ——
    # 放行的话，执行器读不到会自己找补，错误被埋进产物里。
    refs = [a for a in (artifacts or []) if a]
    unknown = [a for a in refs if not artifacts_mod.has(a)]
    if unknown:
        # ⚠️ 用**换行**拼，不用"；" —— 清单每项可能两行（梗概 ＋ 路径），
        #    分号拼会把它们挤成一大坨（`body_lines` 那边会整块缩进，本来就是列表）。
        have = "\n".join(artifact_index_lines()) or "（本会话还没有产物）"
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
        # 它申报要的那条信息（`ask_user` 的原话）—— 新建的 Task 还没申报；
        # 每次 `settle` 由 `absorb_outcome` **覆盖写**（见 `Task.asked`）。
        "asked": "",
        # 平台故障的原因（空 = 没故障）—— 新建的 Task 当然还没故障；
        # 每次 `settle` 由 `absorb_outcome` **覆盖写**（见 `Task.fault`）。
        "fault": "",
        # ⚠️ `artifacts` 是**产出**（产物 ID 列表），`refs` 是**输入引用** —— 两个方向，别混。
        # 待答投递（`graph._feed_waiting`）要按 refs 原样重建可读产物清单。
        "refs": refs,
        # 🔴 **历次问答对，累积在 Task 上**（用户 2026-11-02 定的形状）：
        # `[{"by": "用户"|"主 agent", "q": 执行器问的原话, "a": 答复}]`
        #
        # **为什么是"对"而不是两个平行列表**：执行器用完即弃，每次重入都要重放全部已知；
        # 而 `report` / `asked` 都是**覆盖**写的 —— 答案累积、问题覆盖的话，
        # 第二次重入之后旧答案就没有指涉了（"答第二个把第一个冲掉"的镜像版）。
        #
        # **为什么不分两个字段**（原来是 `answers` ＋ `agent_answer`）：那是**按来源分叉**，
        # 而"谁答的"本来就是每条记录的一个属性 —— 一个列表 ＋ 一个 `by` 字段就够了。
        #
        # 🔴 **`q` 取 `asked`（`ask_user` 的参数原话），不取 `report`**（2026-11-04 修）：
        # 平台**不解析散文**，所以"哪句才是问题"这件事它答不了 —— 正解不是去抽，
        # 是**取那个本来就是问题的字段**。用户答的那条在 `_feed_waiting` 截，
        # 主 agent 试答那条在 `delegate` 里截，两处都取 `asked`。
        "qa": [],
        #: 这轮是谁起的（台账据此说清"全文在哪"）—— `delegate` 这一趟的全文在**回执**里，
        #: 而投递重跑那一趟没有回执（见 `briefing.Task.run_by`）。⚠️ 试答后重跑仍在
        #: **同一次** delegate 调用里，回执也还是这一份 —— 所以这里恒为 "delegate"。
        "run_by": "delegate",
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
    outcome = executor.run_task(skill, render_briefing(rec), rec.get("user_turns"))
    absorb_outcome(rec, outcome)

    # ② 按**三态枚举**分派 —— 穷举，没有第四种（用户「封闭原则」）
    # ⚠️ 状态迁移一律走 `settle()`（**唯一一处**：写字段 + 定 stage）——
    #    原来这里手写 `rec.update(stage=…, artifacts=outcome.artifacts, …)`，
    #    **无条件覆盖**会把"这一轮没产出就不抹掉上一轮产物"的保护当场抵消（实测踩过）。
    if outcome.status == "completed":
        # 🔴 **平台不下"完成了"的结论**（2026-11-05）：三态是**结构算的**
        #    （交没交产物 / 报没报要信息）—— "**够不够**"由主 agent 判：
        #    回执把交付账（产物 ID + 梗概 + 正文入口）摆给它，它自己看 ✓。
        settle(rec, outcome)
        return _out(
            rec, outcome,
            note="**你判**：这次交付够不够？（不够就换个说法重派，或向用户说明）\n"
                 "要核对内容就用 `read_artifact('<产物 ID>')` 读全文 —— 正文不在你的上下文里。\n"
                 "⚠️ 它说的话在上面【执行器说】里，**过一眼** —— 它可能在话里说了什么"
                 "（这次它没调 `ask_user`，平台不替它转达）。",
        )

    if outcome.status == "waiting_for_input":
        # ③ 在要信息 → 平台强制先让主 agent 试答（**两条路共用这一步**，
        #    见 `try_answer_then_rerun` 的 docstring —— 投递重跑那条也走它）
        outcome, answer = try_answer_then_rerun(
            rec, skill, outcome, messages=runtime.state.get("messages") or []
        )

        if outcome.status == "completed":
            # 试答补上了 → 重跑完成（`agent_answer` 让回执把"你补的信息"也带上）
            settle(rec, outcome)
            return _out(rec, outcome, agent_answer=answer)

        if outcome.status == "waiting_for_input":
            settle(rec, outcome)
            trace.note(f"delegate · {tid} 登记待答（平台负责转达，不由主 agent 代问）",
                       kind="waiting")
            return _out(
                rec, outcome,
                note="平台已登记为待答，**并会负责把这个问题转达给用户**，"
                     "用户的答复也会由平台投回给它 —— 你**不用**重复问，"
                     "更不要自己替用户定一个答案。你这条回复说清楚「派了什么、还差什么」就行。",
            )
        # 补了信息之后反而变成 incomplete → 落到下面的 failed 分支

    # ④ 只剩 `incomplete` —— 推了 N 轮，**既没交产物、也没申报要信息**。
    #    按评审人 E2「情况 A：执行失败」→ **交给主 agent 决定**，而不是拿去烦用户。
    #    ⚠️ 平台**不猜**它为什么没交（不读正文 ✓）：可能它真干不了，也可能它把答案
    #    **写在了回复里**（正文交付是反模式，平台不认）—— 两种都由主 agent 看回执决定 ✓。
    settle(rec, outcome)
    # 🔴 **平台故障 ≠ 执行器干不了**（2026-11-04 复审 N2/R8）：故障时那句"推了 N 轮"
    #    是假话（一轮都没跑完），而主 agent 该做的事也完全不同 —— 换说法重派 vs **原样重派**。
    if outcome.fault:
        trace.note(f"delegate · {tid} **平台故障**（重试过仍失败）→ 没跑成",
                   kind="delegate_failed")
        return _out(
            rec, outcome,
            note="这次是**平台故障**，**不是任务本身的问题** —— 原样再 `delegate` 一次即可"
                 "（不用改措辞、不用改参数、不用问用户）。",
        )
    trace.note(f"delegate · {tid} status=incomplete（推了 {outcome.rounds} 轮）→ failed",
               kind="delegate_failed")
    return _out(
        rec, outcome,
        note="它推满了轮次**既没交产物、也没说要什么信息**。两种可能，你看回执定：\n"
             "- 它**真的干不了** → 换个说法再 `delegate` 一次，或向用户说明这次没做成；\n"
             "- 它其实**把答案写在了回复里**（上面【执行器说】）→ 平台不认正文交付，"
             "但你可以**原样转达**给用户，或者让它**交付成产物**再来一次。",
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
        "\n\n🔴 **本轮强制委派**：专业技能的活**一律**用 `delegate` 派出去，"
        "**不要自己在回复里写成品** —— 你手边既没有产出工具，也没有技能原文。"
    ),
    "inline": (
        "\n\n🔴 **本轮不委派**：你手边**没有 `delegate` 工具**，"
        "专业技能的活自己按技能清单和技能原文做。"
    ),
}


class _StopWhenPending(AgentMiddleware):
    """**执行器一申报待答，主 agent 这一拍就不生成**（`discussion-log` F18 的收口）。

    ## 🔴 本质：**那一刻是个决策点，不是对话轮次**（用户 2026-11-05 澄清）

    这一拍要决定的是「**结果回给执行器，还是问用户**」—— 那是个**决策**。
    但 ReAct 循环有一条默认规则：

        工具返回了 → 模型再生成一拍

    它**假设"工具返回之后就是该说话了"** —— 于是把**决策点当成了普通轮次**，
    主 agent 就"发言"了，而它唯一能说的就是复述那个问题。

    ⇒ **正解不是"拦住它的输出"，是"让那一拍不发生"。**

    ⚠️ **"拦截 / 禁言 / 收话"是错的框法**（我在这上面绕了很久）：那听起来像
    **压住一个本该发生的回合**。真实情况是 —— 中间件在**模型调用之前**就跳走了，
    **那次调用根本没发生**，不是"生成了再压住"。

    ## 治的是什么

    用户看到的那个重复：**同一个问题在终端上出现两遍** —— 主 agent 的回复里一遍、
    平台透传一遍。实测（`logs/proto-20261104-152230.log`）**4/4 次全部复述**。

    ## 试过、都不行的六条路（**按轴分两组，别重走**）

    **轴一 —— "让它别写"**（三条全败）：

    | 做法 | 结果 |
    |---|---|
    | 提示词明说"问题平台会贴，你不要写" | ❌ 无效 |
    | 台账/回执**不给**它问题内容 | ❌ 堵不死 —— 回执的【执行器说】经常本身就含问题，而不截断是用户定的 |
    | 台账**给**它问题内容（平台的话、来源正确） | ❌ 照样复述，而且写得更长（把选项排成 A/B/C/D） |

    **轴二 —— "让工具自己结束循环"**（想从工具那一侧收口）：

    | 做法 | 结果 |
    |---|---|
    | `Command(goto=END)` | ❌ `create_agent` 的 `tools→model` 是**条件边**，忽略 `goto` |
    | redirect（`ToolMessage.name` 指向 `return_direct` 工具） | ❌ **ToolNode 会把 `name` 覆盖**成实际执行的工具名 |
    | 动态改 `return_direct` | ⚠️ 有效，但是**共享可变状态** + 依赖框架内部实现 —— 不要 |

    **⇒ 两条轴的共同错误**：都默认了"**那一拍一定会生成**"，于是解全变成"事后处理"。
    只要主 agent 手里有问题内容、又有机会发言，它就会写 —— 三种形态全试过，**4/4 全复述**。

    > 🔴 **出问题的是"它有机会说"，不是"它看得见"。**

    ## 为什么是 `before_model` 中间件（2026-11-04 实测）

    `before_model` 是**模型调用之前**的一个节点，`can_jump_to=["end"]` 让它能直接
    跳到出口 —— 于是**那次模型调用根本不会发生**，主 agent 一个字都不生成。
    ⚠️ 关键：**它是公开 API**（`langchain.agents.middleware`），框架自带的
    `ModelCallLimitMiddleware` / `ToolCallLimitMiddleware` / `PIIMiddleware`
    **用的就是这同一个模式**（`@hook_config(can_jump_to=["end"])`）。

    ## 触发条件是**结构事实**，不是模型判断

    读的是回执对象上的 `handoff.kind`（`Receipt.kind`）——
    `"waiting"` 才停；`completed` / `failed` / `fault` **照常发言**（它要报交付、
    要处置失败）。⚠️ **不读文本、不猜语义**（判据一）。

    ## 谁把问题交到用户手上

    平台。两条路，都不经过 LLM：CLI 的 `next_pending` 提示符、前端的交互卡片；
    外加 `finalize` 注入的那条 `AIMessage`（转写里那条）。
    """

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """这一轮平台要贴一条新问题出去 → 结束这一轮，模型不会被调用。

        **两种形状都要认**（少认一种就是"拦一半"，2026-11-04 实测坐实）：

        **形状一 —— 「本批回执」**（主 agent 自己调的 `delegate`）：
        ⚠️ **要看整批，不能只看最后一条**：模型会**并行调工具** —— 实测它把 `delegate`
        和 `read_skill` 放在**同一条消息**里，于是待答回执**不排在最后**，
        `msgs[-1]` 是 `read_skill` 的返回 → 机制**静默失效**（那次 4 个问题只拦下 1 个）。
        "这一批" = 末尾**连续的那串 `ToolMessage`**（同一条 `AIMessage` 的 `tool_calls`）。

        **形状二 —— 「投递后又问」**（`state["fed_asking"]`，平台在 `prepare` 里投递，
        执行器又问了一个）：这条路上**根本没有 `delegate` 调用** —— 主 agent 的消息末尾是
        `HumanMessage`（用户刚答的那句），上面那轮扫描**扫不到任何工具返回**。
        事实由 `node_prepare` 写在 `AgentState.fed_asking` 上（**本轮信号**，每轮重写）。
        """
        # ── 形状一：本批回执 ──
        for m in reversed(state.get("messages") or []):
            if not isinstance(m, ToolMessage):
                break
            handoff = (m.additional_kwargs or {}).get("handoff") or {}
            if handoff.get("kind") == "waiting":
                return self._stop("这批工具返回里有待答回执")
        # ── 形状二：投递后又问 ──
        if state.get("fed_asking"):
            return self._stop("投递之后执行器又问了一个")
        return None

    @staticmethod
    def _stop(why: str) -> dict[str, Any]:
        """⚠️ **单独报一行**：读日志的人得看得出"主 agent 这一轮被平台收了话" ——
        不然只会以为它没说话（那是另一回事）。`why` 写清是**哪条路**拦下的（复盘要看）。
        """
        trace.note(f"主 agent **这一轮不发言**（{why}，问题由平台原样贴给用户）"
                   " → `before_model` 跳 end", kind="")
        return {"jump_to": "end"}


def build_agent(state_schema: type) -> Any:
    """主 agent：一个完整的 agent（工具 + 循环）。

    ⚠️ **工具集由 `EXEC_PATH` 决定**（默认 `"auto"` = 两条路都开）。
    """
    return create_agent(
        get_model(),
        tools=list(_TOOLS[EXEC_PATH]),
        system_prompt=_MAIN_PROMPT
        + _MODE_NOTE.get(EXEC_PATH, "")
        + "\n\n可用技能：\n"
        + registry.catalog(),
        state_schema=state_schema,
        # 🔴 **待答时不发言**（F18 的收口）—— 见 `_StopWhenPending` 的 docstring。
        middleware=[_StopWhenPending()],
        name="main_agent",
    )
