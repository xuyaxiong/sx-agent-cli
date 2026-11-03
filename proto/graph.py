"""图：prepare（注入台账）→ agent（主 agent 循环）→ finalize（兜底补问）。

**主 agent 是完整的 agent**，流程由它自己驱动。平台在旁边只做两件事：

  - **注入**（prepare）：把**任务台账**（谁在干什么、干到哪了、还缺什么）摆到它面前
    —— **只提供事实，不替它做决定**（"用户是不是在回答"是理解问题，该柔性）
  - **兜底**（finalize）：循环结束后检查待答都问出去了吗，没问就补进回复
    —— 保证**不丢**

三个概念：

    Skill（专业能力 · 静态）──┐
                              ├─► Task（一次具体业务工作）──► Sub Agent（可选执行者 · 用完即弃）
    用户原话 ─────────────────┘

**Task 只在主 agent 决定 delegate 时才建。** 不委派就没有 Task。

⚠️ `Task` 存在 Graph State 里（**不是独立的业务实体**）——它的生命周期就是图执行的一部分，
不跨 thread、不需要独立查询。**什么时候该独立出去**见 `_STAGE_MARK` 上方的注释。
"""

from __future__ import annotations

import operator
import time
from typing import Annotated, Any, NotRequired, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from proto import trace
from proto.agent import build_agent
from proto.handoff.briefing import absorb_outcome, render_briefing
from proto.handoff.ledger import render_ledger


def _merge_tasks(old: list[dict] | None, new: list[dict] | None) -> list[dict]:
    """按 id 合并 Task。

    ⚠️ 必须用 reducer：模型会**并行**发多个 `delegate` 调用，
    每个工具都读到同一份旧 state —— 直接覆盖会**只剩最后一个 Task**（实测踩过：
    三个任务的 id 全变成 `t1`，最终只留下一个）。
    """
    merged: dict[str, dict] = {t["id"]: t for t in (old or [])}
    for t in new or []:
        merged[t["id"]] = t
    return list(merged.values())


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    #: 只记录"主 agent 决定委派出去的工作"。不委派就是空 —— 这是 Task 概念在新设计里的落点
    tasks: NotRequired[Annotated[list[dict[str, Any]], _merge_tasks]]
    log: NotRequired[Annotated[list[str], operator.add]]


class AgentState(TypedDict):
    """主 agent **子图**的状态 —— **故意不含 `log`**。

    🔴 **为什么必须窄**：子图会把它状态里的字段**原样回吐**给父图。
    父图的 `log` reducer 是 `operator.add`，于是父图自己的 log 被子图又加了一遍 ——
    **每轮翻一倍**：`1 → 5 → 12 → 27 → …`（指数）。

    实测（`experiments/probe_double_prepare.py`）：
    `prepare` 明明只返回 3 条，`finalize` 进来却看到 8 条 —— 中间 `agent` 把 4 条又加了一次。

    ⚠️ **这是既有 bug，不是新引入的**：改动前的 `baseline` 日志就是 `[1, 5, 12]`。
    以前没暴露，是因为那些 run 走了"主 agent 先问用户、不派发"的路径，
    `prepare` 直接返回 `{}`，**一条 log 都不产生**。

    `tasks` 不受影响 —— 它的 reducer 按 id 合并，是幂等的。
    """

    messages: Annotated[list[AnyMessage], add_messages]
    tasks: NotRequired[Annotated[list[dict[str, Any]], _merge_tasks]]


# ──────────────────────────── prepare：注入待答 ────────────────────────────


def _last_human(state: State) -> str:
    for m in reversed(state.get("messages") or []):
        if isinstance(m, HumanMessage) and str(m.content).strip():
            return str(m.content)
    return ""



def _feed_waiting(state: State) -> tuple[list[dict], list[str]]:
    """把用户这条消息**原话**投给所有还挂着的待答任务。

    🔴 **不做任何判断** —— 不判断"用户是不是在回答""答的是哪一条"。

    用户明确要求（2026-10-29）：
    > "只要是答复了 就直接恢复 subagent 就行啊" · "答非所问就答非所问呗"

    原话投过去，执行器自己判断够不够；不够就再问一遍。
    **平台不替用户判断他的话算不算回答** —— 那是语义判断，而且判断错了
    比"多问一遍"更糟（F7 就是这么丢的）。

    这也是**待答唯一的解除路径**：投递后要么 completed，要么还在等。
    """
    from proto.executor import run_task
    from proto.skills import SKILLS

    user = _last_human(state)
    waiting = [
        t for t in (state.get("tasks") or []) if t.get("stage") == "waiting_for_input"
    ]
    if not user or not waiting:
        return [], []

    out: list[dict] = []
    logs = [f"prepare: 待答 {len(waiting)} 条 → 把用户这条消息**原话**投过去（不判断是不是回答）"]
    for t in waiting:
        skill = SKILLS.get(t["skill"])
        if skill is None:
            logs.append(f"prepare: ⚠️ {t['id']} 的能力「{t['skill']}」不存在，跳过")
            continue

        # 🔴 **历次答复必须累积**（2026-10-29 人在环实测暴露）：
        # 执行器是隔离上下文、**用完即弃**，每次投递都是一次全新的 run_task。
        # 只带"最新一条"的话，用户一次答一个时，**答第二个就把第一个冲掉了** ——
        # 实测现象：问"用途"→答"课前预习"→问"填写形式"→答"A"→**又问一遍"用途"**，
        # 用户被同一个问题问两次（`_feed_waiting` 的 R03/R04/R05 三次投递，每次都只带当轮）。
        # 脚本化跑批测不出来：预置答复是一句话把两问都答了。
        # 答复**累积在 Task 上**，整份交接书一起喂 —— 见 `agent.render_briefing`
        # 🔴 **一问一答成对存**（用户 2026-11-02）：`report` 是**覆盖**写的
        # （`absorb_outcome`），只存答案的话，第二次投递之后旧答案就没有指涉了 ——
        # 用户看到的是"答：A"，却看不到 A 在回答什么。**问题在这一刻从 Task 上
        # 原样截下**（此刻的 `report` 就是它问的那句），平台搬，不靠 LLM 转述。
        rec = {**t, "qa": [*(t.get("qa") or []),
                           {"by": "用户", "q": t.get("report") or "", "a": user}]}
        trace.section(f"prepare · 投递用户原话 → {t['id']} · {t['skill']}", kind="feed")
        trace.block("投过去的原话", user)
        trace.note(f"累计问答 {len(rec['qa'])} 对（历次一起带上，否则执行器会重问）")
        # 交接关键日志：投递这一跳（交接书正文由 `run_task` 那边记）
        trace.handoff("答复投递", "平台", "执行器", user,
                      structured=f"Task.qa 追加 1 对（累计 {len(rec['qa'])} 对）")
        # ⚠️ `refs` 要一起带上：不重建可读产物清单，执行器在续跑时就"看不见"前序产物了
        # 用户原话也一起带上（快照在 Task 上，所以这条重入路径**自动**有它）
        outcome = run_task(skill, render_briefing(rec), rec.get("user_turns"))
        absorb_outcome(rec, outcome)
        if outcome.status == "completed":
            rec.update(stage="completed", artifacts=outcome.artifacts)
            logs.append(
                f"prepare: ✅ {t['id']}（{t['skill']}）答复后**完成**"
                f"　产物={outcome.artifacts or '（在回复里）'}"
            )
        elif outcome.status == "waiting_for_input":
            logs.append(f"prepare: ⏳ {t['id']}（{t['skill']}）答复后**还是缺信息** → 仍待答")
        else:
            rec.update(stage="failed")
            logs.append(f"prepare: ❌ {t['id']}（{t['skill']}）答复后仍未完成 → failed")
        out.append(rec)
    return out, logs


def node_prepare(state: State) -> dict:
    """① 把用户原话投给待答任务（见 `_feed_waiting`）→ ② 注入任务台账。

    **顺序不能反**：先投递，注入的台账才反映投递后的新状态
    （任务可能刚从"等答复"变成"已完成"）—— 主 agent 看到的是最新的，就不会重复派。

    **只提供事实，不替它做决定**（"要不要重跑"是理解问题，该柔性）。

    为什么要注入：不注入的话，主 agent 只看得到自己对话历史里那几条工具返回，
    **不知道已经派过什么** —— 实测后果就是"用户补一句，它把三个任务全部重跑"（S14）。

    ⚠️ **目前是全量注入**（用户确认）。若台账变长到撑上下文，
    可改成只注入活跃的（进行中 / 待答 / 最近完成的），老归档不注入。
    """
    updated, logs = _feed_waiting(state)

    # 投递后的最新台账（reducer 按 id 合并，这里先自己合一遍，好把最新状态渲染出来）
    merged = {t["id"]: t for t in (state.get("tasks") or [])}
    for t in updated:
        merged[t["id"]] = t
    tasks = list(merged.values())
    if not tasks:
        return {"log": logs} if logs else {}

    # 台账怎么渲染在 `proto/handoff/ledger.py`（schema 与模板放一起）
    waiting = [t for t in tasks if t.get("stage") == "waiting_for_input"]
    text = render_ledger(tasks)
    # 🔴 **注入什么就记什么**（用户 2026-11-03：*"这些在日志中得完整记录下来便于复盘"*）。
    # 原来这里只记了"注入 N 项"，**正文一个字都不在日志里** —— 而它是平台每轮塞给
    # 主 agent 的东西，复盘时最该看到的就是它。见 `trace.handoff`。
    trace.handoff("台账", "平台", "主 agent", text,
                  structured=f"Task × {len(tasks)} → render_ledger", full=True)
    return {
        "messages": [SystemMessage(content=text)],
        "tasks": updated,  # reducer 按 id 合并
        "log": logs + [f"prepare: 注入台账（{len(tasks)} 项，其中待答 {len(waiting)} 条）"],
    }


# ──────────────────────────── finalize：兜底补问 ────────────────────────────


def node_finalize(state: State) -> dict:
    """**透传**：待答原样贴进回复，保证问题一定到用户手上。

    🔴 **这里原来是 LLM 判断**（`_which_unasked`：问"主 agent 问出去了吗"，
    没问出去才补）。已删除 —— 用户明确要求（2026-10-29）：

    > "能用代码用机制保证的 一定不要用 llm 自行决策"

    那个判断**两头都会错**：判"问出去了"其实没问 → 问题静默丢失；
    判"没问出去"其实问了 → 用户看到重复。而且它还有个更糟的后果 ——
    它让"问题会不会到用户手上"这件事**依赖 LLM**，而这是平台该保证的事。

    **现在的分工**：
    - **保证**（代码）：有待答就原样贴出去，无条件
    - **礼貌**（提示词）：`prepare` 会告诉主 agent "平台负责转达，你不用重复问"
      —— 它照做了就没有重复；没照做也只是重复一句，**不影响不丢**

    ⚠️ **产物路径不在这里贴** —— 那条信息顺着 `delegate` 的返回走
    （见 `agent.render_receipt` 的【产出的产物】）：路径是 `write_artifact` 的返回里就有的，
    平台把它**顺着原路带上去**就够了，不需要另起一套注入。
    """
    waiting = [t for t in (state.get("tasks") or []) if t.get("stage") == "waiting_for_input"]
    if not waiting:
        return {}

    block = "\n\n".join(t["report"] for t in waiting)
    # ⚠️ **只发内容**：`finalize: ` 那个前缀是**日志文件格式**的一部分，归记录器
    # （`trace._NOTE_HEAD`）—— 业务侧带上它，展示层就得按位置切回来（审核 F1）。
    trace.note(f"透传 {len(waiting)} 条待答（平台直接贴，不判断主 agent 问没问）",
               kind="relay")
    # 🔴 **已知膨胀点（判据③）**：这段和**回执的【执行器说】**是**同一段话** ——
    # 一轮之内它会进主 agent 的上下文**两次**（回执一次、这里一次）。
    # 实测（2026-11-03）：两条 report 合计 936 字，各出现 2 次。
    # **不能靠"去重"解决** —— 那需要判断"主 agent 说没说"，是 LLM 判断（2026-10-29 已否决）。
    # 要优化的话，方向是**让主 agent 知道自己不必复述**，而不是平台少贴。
    # 见 `docs/handoff-audit.md` §四 膨胀账。
    # 🔴 同台账：**注入什么就记什么** —— 这段原来只在 `_run.md` 里有（走快照），
    # trace 里一个字都没有，复盘时看不到"平台替执行器转达了什么"。
    trace.handoff("待答透传", "平台", "用户", block,
                  structured=f"Task.report × {len(waiting)} → AIMessage(injected_by=platform.finalize)",
                  full=True)
    return {
        # 🔴 **打标记**：这条 AIMessage 的内容是**执行器的原话**，不是主 agent 说的 ——
        # 不打标记的话，记录层会把它标成"🤖 主 agent 输出（它自己说的话）"，
        # **来源标注在这一跳就断了**（2026-11-03 实测）。
        # 标记挂在 `additional_kwargs` 上（实测不会漏给模型），记录层读它 ——
        # 而不是"看内容像不像执行器的话"（那是反结构化，见 `proto/handoff` 的 docstring）。
        "messages": [AIMessage(content=block, additional_kwargs={"injected_by": "platform.finalize"})],
        "log": [f"finalize: 透传 {len(waiting)} 条待答（原样贴执行器的话，无条件）"],
    }


# ──────────────────────────── 装配 ────────────────────────────


def build():
    g = StateGraph(State)
    g.add_node("prepare", node_prepare)
    main_agent = build_agent(AgentState)  # 主 agent：完整的 agent（工具 + 循环）

    def node_agent(state: State, config: RunnableConfig) -> dict:
        """跑主 agent 子图，**每走一步就把它露出来**。

        🔴 **为什么必须在这里记**：主 agent 的消息**不进** `run_task` 的轨迹 dump
        （那是执行器的），所以它的 `delegate` / `read_artifact` / `write_artifact`
        在日志里原来**一点痕迹都没有**。实测踩过：一个 case 里主 agent 读了 5162 字的产物，
        日志里查不到，只能从"3 个 ToolMessage 但只有 2 次 delegate"反推 ——
        **日志的职责是让这种事直接可查，不靠推断。**

        🔴 **为什么是 `stream` 而不是 `invoke`**（2026-10-30 改）：`invoke` 会把**整个
        agent 循环**跑完才返回，于是"模型→工具→模型→工具…"这一串**全程静默**。
        实测一次真实会话：主 agent 连调 8 个工具、**336 秒里日志一行不动** ——
        而 CLI 是 tail 日志的，用户侧就是**黑屏**，分不清"在干活"还是"卡住了"。

        改成 `stream(stream_mode="values")` 后，**每个 super-step 结束就 dump 一次**
        （模型那步 → 它的工具调用；工具那步 → 返回了什么）。静默窗口从"整个循环"
        缩到"一次 LLM 调用"。

        ⚠️ **这不是加日志，是把已经发生的事更早露出来** —— 行为一点没变，
        返回的仍是最终 state（`stream` 的最后一个 chunk）。
        """
        out: dict = {}
        prev_n = len(state.get("messages") or [])
        logs: list[str] = []
        step = 0
        # `stream_agent` 顺带把**每个字**也写进日志（攒到换行写一行）——
        # 一次 LLM 调用那几十秒不再是黑屏。
        for chunk in trace.stream_agent(main_agent, state, config, "主 agent",
                                        role=trace.AGENT):
            out = chunk
            msgs = chunk.get("messages") or []
            new = msgs[prev_n:]
            if not new:
                continue  # 第一个 chunk 是输入 state 本身，没有新消息
            prev_n = len(msgs)
            step += 1
            trace.dump_trace(f"主 agent 第 {step} 步", new, role=trace.AGENT)
            # 同一批调用**也进 state 的 log** —— 逐轮审计文件（`_run.md`）读的是它，
            # 只写 trace 文件的话，审计时还得两个文件对着看。
            for name, args in trace.tool_calls_of(new):
                line = f"[工具] 主 agent → {name}（{args}）"
                trace.note(line)
                logs.append(line)
        if not out:  # 理论上不会（stream 至少吐一次最终 state）—— 兜一下，别把状态弄丢
            return main_agent.invoke(state, config)
        return {**out, "log": logs} if logs else out

    g.add_node("agent", node_agent)
    g.add_node("finalize", node_finalize)
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "agent")
    g.add_edge("agent", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=InMemorySaver())
