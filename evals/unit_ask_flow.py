"""单元级：**「问」这条链的记录**（不调 LLM）—— 每一跳写清「谁 → 谁：什么」。

    uv run python -m evals.unit_ask_flow

用户 2026-11-05：

> *"这个请求信息相关的 5 条，含义太模糊了，没头没脑了，难以阅读审计。"*
>
> *"朴素来说，问问题你知道吧 —— 关注**谁在向谁问一个什么问题**，你记录必须把这个
>   搞清楚；**谁答不上被转发给谁了**；**谁又答复了谁的什么问题**。"*

🔴 **一次「问」从生到死是四次交接**：

    ① 要信息   执行器 → 平台      它要什么（`ask_user` 的参数原话）
    ② 试答     平台 → 主 agent    "你的上下文里有没有"（答不上才往下走）
    ③ 待答透传 平台 → 用户        把它问的那句**原样**转达
    ④ 答复投递 用户 → 执行器      用户答的是**它问的那句**

**守什么**（每一条都对应一种实测踩过的"读不出来"）：

| 症状 | 守卫 |
|---|---|
| 那一跳**根本没记**（`ask_user` 原来只活在 `🔧 TOOL` 那行里） | ① 四跳都是 `📤 交接`，方向对 |
| **一行干两件事**：run 结束那行是"账"，却塞了 132 字原话 | ② `note` 只报账、不搬内容 |
| 反过来：为了不重复而**把话丢了**（交接三问②） | ③ 那句话一个字都不丢 |
| 把"接下来"说成"已经"（`🔙 RETURN` 原来写的是**流程说明**） | ④ 工具返回在试答**之前** |
| 同一个词指两件事（`R01` 和 `轮次=2` 并排） | ⑤ 两个编号各自带名字 |

⚠️ **有效性自检**（把哪一处改回去，哪一条必须红）：

    - 删掉 `executor.run_task` 里的 `trace.handoff("要信息", …)`        → ① 红
    - run 结束那行改回 `+ (f"｜**申报要信息**：{outcome.asked}" …)`       → ② 红
    - `agent._ask_main` 的【前提】里把 `{outcome.asked}` 拿掉            → ③ 红
    - `make_ask_user` 的返回改回「平台会先问主 agent，它答不上再转用户」  → ④ 红
    - 轮次行改回 `f"run_task 第 {n} 轮：…"`                             → ⑤ 红
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import asyncio

import sys
from typing import Callable

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os

# 🔴 **单测的日志打 tag**（2026-11-06）：`logs/` 里真会话与测试垃圾混在一起，复盘时
#    grep 不动 —— 打上 tag 后文件名是 `proto-<ts>-unit-<名字>.log`，一眼可辨
#    （清理：`rm logs/*-unit-*.log`）。复用跑批那个 `PROTO_LOG_TAG`（`evals/_child.py`）。
# ⚠️ **必须排在所有非标准库 import 之前**：`proto.trace` 在 import 那一刻读 env 定文件名，
#    而 `from app import chat` 这种**间接** import 也会把它拉进来（实测踩过：
#    第一版插在 `from app import chat` 之后 → 日志照样没 tag）。
#    守卫：`unit_feed_waiting` ⑮。
os.environ["PROTO_LOG_TAG"] = "unit-ask-flow"
os.environ["PROTO_LOG_DIR"] = "tmp/logs"   # 🔴 测试日志是**噪音** —— 别混进给人看的 `logs/`

from proto import graph as _graph

from proto import recorder, trace

def _prepare(state):
    """`node_prepare` 现在是**协程** —— 测试里的同步收口。"""
    return asyncio.run(_graph.node_prepare(state))


def _finalize(state):
    """`node_finalize` 现在是**协程** —— 同上。"""
    return asyncio.run(_graph.node_finalize(state))


#: 这次「问」的原话 —— 断言拿它当指纹（全文出现在哪、不出现在哪）。
Q = "这节课你希望侧重哪种上法？（A. 探究发现型 B. 讲解示范型 C. 练习巩固型）"
TASK = "生成人教版小学语文《荷花》一课的教案。请按标准教案结构输出。"
TID = "t86e687aa"

#: 这条链**该有的四跳** —— `(跳名, 从, 到)`。少一跳 = 那段记录**读不出来**。
_CHAIN: list[tuple[str, str, str]] = [
    ("要信息", "执行器", "平台"),
    ("试答", "平台", "主 agent"),
    ("待答透传", "平台", "用户"),
    ("答复投递", "用户", "执行器"),
]

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail and not ok else ""))


def sample_task() -> dict:
    """样例场景的那个 Task（**守卫和 `experiments/ask_flow_log` 共用** —— 一处定义）。"""
    return {
        "id": TID, "skill": "lesson-prep", "task": TASK,
        "stage": "waiting_for_input", "artifacts": [], "refs": [],
        "asked": Q, "report": "好的，第一个问题已发出，等你的答复。",
        "run_by": "delegate",
        "user_turns": [{"n": 1, "text": "帮我备《荷花》这一课", "after": ""}],
        "qa": [],
    }


class _Runtime:
    """`delegate` 只要两样：`tool_call_id` 和 `state`（真 runtime 也是这两个口）。"""

    tool_call_id = "call_86e687aa"

    def __init__(self, **state: object) -> None:
        self.state = state


def drive(on_phase: Callable[[str, list[dict], str], None] | None = None) \
        -> list[tuple[str, list[dict], str]]:
    """**真跑一遍**「问」这条链 → `[(阶段名, 这一步的事件, 这一步落盘的行)]`。

    ⚠️ **打桩的只有"模型会吐什么"**（`get_model` / `create_agent` / `stream_agent` /
    `safe_structured` / `executor.run_task` 的返回值）—— **调用点、渲染、落盘、
    事件全是真的**。所以下面这些行**就是**真跑出来的那几行，不是手写的样例。

    ⚠️ 它**会往真日志文件里写**（同 `unit_decision`）—— 记录层的行为本身也在被测。

    `on_phase`：**每一跳跑完就回调一次**（`experiments/ask_flow_log` 用它边跑边打印 ——
    等整个 `drive()` 返回再打印的话，展示层那几行会全挤在最后，看着像"顺序倒了"）。
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from proto import executor as ex
    from proto.registry import registry
    from proto.skills import Skill

    registry.replace({"lesson-prep": Skill(
        name="lesson-prep", description="备一节课的教案",
        instructions="（技能指令 2532 字，此处略）")})

    events: list[dict] = []
    trace.subscribe(events.append)
    out: list[tuple[str, list[dict], str]] = []

    def _phase(title: str, start: int, n_ev: int) -> int:
        """收这一阶段：新落盘的那几行 ＋ 新发的那些事件。"""
        # ⚠️ **文件可能还没建**（日志是惰性的：没写过就没有）—— 见 `recorder.log_size`
        lines = ""
        if recorder.LOG_FILE.exists():
            with open(recorder.LOG_FILE, "rb") as f:
                f.seek(start)
                lines = f.read().decode("utf-8", "replace")
        out.append((title, events[n_ev:], lines))
        if on_phase is not None:
            on_phase(title, events[n_ev:], lines)
        return recorder.log_size()

    # ── ① 执行器那一侧：`run_task` 真跑，只把模型和图打桩 ──────────────────────
    box: dict = {}
    real = (ex.get_model, ex.create_agent, trace.stream_agent)

    def _fake_agent(*_a, tools=None, **_k):  # noqa: ANN002, ANN003
        box["tools"] = tools
        return object()

    async def _fake_stream(_agent, state, _config, _who, *, role):  # noqa: ANN001
        """执行器那一轮：**真调一次 `ask_user`**（就是它会做的那件事），然后这一轮结束。

        ⚠️ 得是 **async** 生成器 —— `stream_agent` 现在是异步的（asyncio 迁移第 2 步）。
        """
        ask = next(t for t in box["tools"] if t.name == "ask_user")
        # ⚠️ **`await`，不是 `asyncio.run`** —— 这个假流本身就是**协程**，跑在事件循环里，
        #    再 `asyncio.run` 会报 "cannot be called from a running event loop"，
        #    于是 `run_task` 走故障分支、**dump 根本不发生**（实测踩到）。
        ret = await ask.ainvoke({"question": Q})
        yield {"messages": [*state["messages"],
                            AIMessage("", tool_calls=[{"name": "ask_user", "id": "call_ask",
                                                       "args": {"question": Q}}]),
                            ToolMessage(ret, tool_call_id="call_ask")]}

    at, n = recorder.log_size(), len(events)
    ex.get_model = lambda **_k: object()  # type: ignore[assignment]
    ex.create_agent = _fake_agent  # type: ignore[assignment]
    trace.stream_agent = _fake_stream  # type: ignore[assignment]
    try:
        outcome = asyncio.run(ex.run_task(registry.get("lesson-prep"), "（交接书正文……）"))
    finally:
        ex.get_model, ex.create_agent, trace.stream_agent = real  # type: ignore[assignment]
    at = _phase("① 执行器 → 平台：它要什么（`ask_user`）", at, n)

    # ── ② 主 agent 那一侧：`delegate` 真跑（试答 → 答不上 → 登记待答）─────────
    import proto.agent as ag
    from proto import llm as llm_mod

    class _Structured:
        """`with_structured_output(schema).invoke(prompt)` —— 只吐一个保守的答案。

        ⚠️ **桩打在"模型"这一层，不打在 `safe_structured` 上** —— 后者还要发
        `section(ask_main)`（标题）和 PROMPT 块（`llm_io`），那两件是**真被测的**：
        展示层把 PROMPT 和四段合成**一块**就靠它们（2026-11-05）。
        """

        def with_structured_output(self, schema):  # noqa: ANN001
            return self

        def invoke(self, prompt):  # noqa: ANN001
            return ag._AgentAnswer(can_answer=False)

    # 主 agent 的上下文**照真会话那个形状造**（2026-11-05）：上一轮的回执（一张单子）
    # 和平台注入的台账（另一张单子）都在里面 —— **多行消息的嵌套**就是靠它们才跑得到。
    # ⚠️ 只喂单行消息的话，"一张单子塞进列表项"那个形状**根本不会出现**，
    #    守卫会绿着放它过去（这正是它漏掉那个 bug 的原因）。
    from langchain_core.messages import SystemMessage

    from proto.handoff.ledger import render_ledger
    from proto.handoff.receipt import build_receipt, render_receipt

    prev_receipt = render_receipt(build_receipt(
        ex.ExecOutcome(status="waiting_for_input", artifacts=[], rounds=1,
                       report="已登记。等待用户答复第一个问题后，我再继续第二个确认。",
                       asked="这节课您希望侧重哪种上法？（A. 探究发现型 B. 讲解示范型 C. 练习巩固型）"),
        note="平台已登记为待答，**并会负责把这个问题转达给用户**，"
             "用户的答复也会由平台投回给它 —— 你**不用**重复问，更不要自己替用户定一个答案。"))
    ledger = render_ledger([{**sample_task(), "stage": "completed", "artifacts": ["art_001"]}])

    real_model, real_run = llm_mod.get_model, ex.run_task
    n = len(events)
    llm_mod.get_model = lambda: _Structured()  # type: ignore[assignment]
    async def _stub_outcome(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        return outcome
    ex.run_task = _stub_outcome  # type: ignore[assignment]
    try:
        asyncio.run(ag.delegate.coroutine("lesson-prep", TASK, _Runtime(messages=[
            HumanMessage("我现在想生成人教版小学语文荷花这一课的教案和导学单，"
                         "注意导学单内容得参考教案内容"),
            AIMessage("好的，我先来生成教案。"),
            ToolMessage(prev_receipt, tool_call_id="call_prev"),
            SystemMessage(ledger),
            HumanMessage("探究发现型"),
        ], tasks=[])))
    finally:
        llm_mod.get_model, ex.run_task = real_model, real_run  # type: ignore[assignment]
    at = _phase("② 平台 → 主 agent：你知道它要的那句吗（试答）；答不上 → 登记待答", at, n)

    # ── ③ 平台 → 用户：透传（`node_finalize` 真跑）────────────────────────────
    from proto.graph import _feed_waiting, node_finalize

    n = len(events)
    _finalize({"tasks": [sample_task()], "messages": []})
    at = _phase("③ 平台 → 用户：把它问的那句原样转达（透传）", at, n)

    # ── ④ 用户 → 执行器：答复投递（`_feed_waiting` 真跑）──────────────────────
    n = len(events)
    async def _stub_completed(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        return ex.ExecOutcome(
            status="completed", artifacts=["art_001"], rounds=2,
            report="教案已生成并交付。")
    try:
        asyncio.run(_feed_waiting(
            {"tasks": [sample_task()], "messages": [HumanMessage("探究发现型")],
             "answer_to": TID}))
    finally:
        ex.run_task = real_run  # type: ignore[assignment]
    _phase("④ 用户 → 执行器：答复的是**它问的那句**（投递重跑）", at, n)
    return out


def main() -> None:
    phases = drive()
    events = [e for _, evs, _ in phases for e in evs]
    lines = "\n".join(t for _, _, t in phases)

    print("① 这条链是**四次交接** —— 每一跳都是 `📤 交接`，方向对")
    hops = {e["hop"]: (e["frm"], e["to"]) for e in events if e["type"] == "handoff"}
    for hop, frm, to in _CHAIN:
        check(f"`{hop}`（{frm} → {to}）记下来了", hops.get(hop) == (frm, to),
              f"实际={hops.get(hop) or '（没有这一跳）'}")

    print("\n② 单行记录（`note` / `section`）**只报账、不搬内容**")
    # 🔴 原来 run 结束那行是"账"（第几轮、什么状态、多久），却塞了 132 字原话 ——
    #    摘要里突然出现内容，读的人分不清"这行到底是结论还是内容"（用户："没头没脑"）。
    moved = [str(e.get("text") or e.get("title") or "")[:46]
             for e in events if e["type"] in ("note", "section")
             and Q in str(e.get("text") or e.get("title") or "")]
    check("没有任何一行搬了那句问题（它只该出现在该给全的地方）", not moved,
          f"搬了的：{moved}")

    print("\n③ 反过来 —— 那句话**一个字都不丢**（交接三问②：切了必须标，这里是不许切）")
    check("在 `🔧 TOOL` 那行（`ask_user` 的参数原话 —— 事实源）",
          any(Q in str(c.get("args")) for e in events if e["type"] == "dump"
              for m in e["messages"] for c in (getattr(m, "tool_calls", None) or [])))
    check("在试答的【前提】里（**这一跳问的就是它** —— 不给全，「答不上」就没指涉）",
          any(Q in "".join(str(x) for x in (e.get("given") or []))
              for e in events if e["type"] == "decision"))
    check("在交接全文里（`full=True` 那一跳 —— 用户要答的就是它）",
          any(Q in str(e.get("text") or "") for e in events
              if e["type"] == "handoff" and e.get("full")))

    print("\n④ 时序：`ask_user` 的返回在**试答之前** —— 所以它只能说「已登记」")
    # 🔴 工具返回那一刻，**试答还没发生** —— 原来那行写「平台会先问主 agent，它答不上
    #    再转用户」，读的人把"接下来"当成了"已经"（用户：含义模糊）。
    i_ret = next((i for i, e in enumerate(events) if e["type"] == "dump" and any(
        type(m).__name__ == "ToolMessage" and "登记" in str(m.content)
        for m in e["messages"])), -1)
    i_dec = next((i for i, e in enumerate(events) if e["type"] == "decision"), -1)
    check("工具返回**早于**试答（结构事实 —— 它不可能知道试答的结果）",
          -1 < i_ret < i_dec, f"返回@{i_ret}，试答@{i_dec}")
    ret = next((str(m.content) for e in events if e["type"] == "dump"
                for m in e["messages"] if type(m).__name__ == "ToolMessage"
                and "登记" in str(m.content)), "")
    check("它说的是**这一次登记了什么**（「已登记为待答」）",
          "已登记为待答" in ret, repr(ret[:70]))
    check("它**不**把「接下来」说成「已经」（没有「平台会先问主 agent」这句流程说明）",
          "平台会先问主 agent" not in ret, repr(ret[:70]))

    print("\n⑤ 两个编号**各自带名字**（`R01` 和 `内部 N 轮` 不是一回事）")
    rounds = [str(e["text"]) for e in events
              if e["type"] == "note" and e.get("kind") == "ask"]
    ends = [str(e["text"]) for e in events
            if e["type"] == "note" and e.get("kind") == "run_end"]
    check("轮次行写的是 `R01 · 内部第 N 轮`（不是光秃秃的「第 N 轮」）",
          bool(rounds) and all("· 内部第" in t for t in rounds), repr(rounds))
    check("结尾那行**不再**把 `R01` 和 `轮次=` 并排放（同一个词指两件事）",
          bool(ends) and all("轮次=" not in t for t in ends), repr(ends))
    check("两行都在**同一份记录**里（守卫读的是落盘的行，不是内存里的对象）",
          all(t.split("] ", 1)[-1] in lines for t in (*rounds, *ends)))

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
