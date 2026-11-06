"""单元级：**执行器一申报待答，主 agent 那一拍不生成**（`discussion-log` F18 的收口，不调 LLM）。

    uv run python -m evals.unit_stop_on_pending

**为什么要有它**：F18（"待答被说两遍"）2026-10-29 发现，2026-11-04 收口。

🔴 **本质**（用户 2026-11-05 澄清）：**那一刻是个决策点，不是对话轮次** ——
要决定的是「结果回给执行器，还是问用户」。但 ReAct 的默认规则是
「工具返回了 → 模型再生成一拍」，它**把决策点当成了普通轮次** → 主 agent 就复述了。
⇒ **正解不是"拦住它的输出"，是"让那一拍不发生"。**

中间试过**六条路**（按轴分两组，**别重走**）：

| 轴 | 做法 | 实测 |
|---|---|---|
| 让它别写 | 提示词明说"问题平台会贴，你不要写" | ❌ 无效 |
| 让它别写 | 台账/回执**不给**它问题内容 | ❌ 堵不死（回执的【执行器说】经常含问题） |
| 让它别写 | 台账**给**它问题内容（平台的话、来源正确） | ❌ 照样复述，写得更长（4/4） |
| 工具结束循环 | `Command(goto=END)` | ❌ `create_agent` 的条件边忽略 `goto` |
| 工具结束循环 | redirect（`ToolMessage.name` 指向 `return_direct` 工具） | ❌ ToolNode 把 `name` 覆盖了 |
| 工具结束循环 | 动态改 `return_direct` | ⚠️ 有效，但共享可变状态 —— 不要 |

**两条轴的共同错误**：都默认了"那一拍一定会生成"，于是解全变成"事后处理"。
最后靠 `agent._StopWhenPending`（`before_model` 中间件 + `jump_to="end"`）：
**那次模型调用根本不发生**，所以没有人复述。**问题出在"它有机会说"，不在"它看得见"。**

⚠️ **⑥ 是那个机制唯一的凭据**：它是**框架行为**（`create_agent` 的中间件边读
`can_jump_to` / `jump_to`），框架一升级就可能悄悄失效 —— 那时模型会被多调一次、
问题又变两遍，而**没有别的地方会红**。

⚠️ **不调 LLM**：假模型 + 假回执，毫秒级。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import sys
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os
# 🔴 **单测的日志打 tag**（2026-11-06）：`logs/` 里真会话与测试垃圾混在一起，复盘时
#    grep 不动 —— 打上 tag 后文件名是 `proto-<ts>-unit-<名字>.log`，一眼可辨
#    （清理：`rm logs/*-unit-*.log`）。复用跑批那个 `PROTO_LOG_TAG`（`evals/_child.py`）。
# ⚠️ **必须排在所有非标准库 import 之前**：`proto.trace` 在 import 那一刻读 env 定文件名，
#    而 `from evals import chat` 这种**间接** import 也会把它拉进来（实测踩过：
#    第一版插在 `from evals import chat` 之后 → 日志照样没 tag）。
#    守卫：`unit_feed_waiting` ⑮。
os.environ["PROTO_LOG_TAG"] = "unit-stop-on-pending"

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.types import Command

from proto.agent import _StopWhenPending

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail else ""))


#: 假模型：第一拍发工具调用，之后每一拍都发正文 —— **它被调几次，就是"主 agent 说了几次话"**
_CALLS: list[int] = []

#: 第一拍**并行**发几个工具调用（`delegate` ＋ 另一个）—— 见 ⑤ 那条守卫
_PARALLEL = {"v": False}


class _FakeModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools: Any, **kw: Any) -> Any:  # noqa: ANN401
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw: Any) -> ChatResult:  # noqa: ANN001, ANN003
        _CALLS.append(1)
        n = len(_CALLS)
        if n == 1:
            calls = [{"name": "delegate", "args": {}, "id": "c1"}]
            if _PARALLEL["v"]:
                # 🔴 **实测踩到的形状**：模型把 `delegate` 和 `read_skill` 放在**同一条消息**里
                #    → ToolNode 按顺序执行 → 待答回执**排在这批的中间/最前**，不是最后一条。
                calls.append({"name": "read_skill", "args": {}, "id": "c2"})
            msg = AIMessage(content="", tool_calls=calls)
        else:
            msg = AIMessage(content=f"第 {n} 拍：主 agent 的正文")
        return ChatResult(generations=[ChatGeneration(message=msg)])


#: 回执的 `kind` —— 由每一节改写（`Receipt.kind` 的取值）
_KIND = {"v": "waiting"}


@tool
def delegate() -> Command:
    """派活（**假替身**，不碰真的 `delegate`）。"""
    return Command(update={"messages": [ToolMessage(
        content="回执", tool_call_id="c1",
        # ⚠️ **就是真回执挂的那个字段**（`agent._cmd` 的 `additional_kwargs["handoff"]`）——
        #    守卫要断的是"中间件读得对"，就得喂**同一个形状**。
        additional_kwargs={"handoff": {"kind": _KIND["v"]}})]})


@tool
def read_skill() -> str:
    """另一个普通工具（用来复现"并行调用"那个坑）。"""
    return "技能原文"


def _run(kind: str, *, parallel: bool = False) -> tuple[int, str]:
    """跑一遍假 agent，返回（模型被调用几次，最后一条消息的类型名）。"""
    _CALLS.clear()
    _KIND["v"] = kind
    _PARALLEL["v"] = parallel
    g = create_agent(_FakeModel(), tools=[delegate, read_skill], system_prompt="x",
                     middleware=[_StopWhenPending()])
    out = g.invoke({"messages": [{"role": "user", "content": "go"}]})
    return len(_CALLS), type(out["messages"][-1]).__name__


def main() -> None:  # noqa: C901
    print("① 🔴 待答 → **主 agent 那一拍不生成**（F18 的收口）")
    n, tail = _run("waiting")
    check("`kind='waiting'`：模型**只被调 1 次**（第二拍根本没发生）", n == 1, f"被调了 {n} 次")
    check("这一轮**没有主 agent 的正文**（末尾是工具回执，不是 AIMessage）",
          tail == "ToolMessage", f"末尾是 {tail}")

    print("\n② 其余出口**照常发言**（别误伤）")
    # ⚠️ 这条是**反面**：中间件只认 `waiting`。`completed` 要报交付、`failed` 要处置失败、
    #    `fault` 要原样重派 —— 那三种主 agent **必须**能说话。
    for kind in ("completed", "failed", "fault"):
        n, tail = _run(kind)
        check(f"`kind={kind!r}`：模型**被调 2 次**（主 agent 正常发言）", n == 2,
              f"被调了 {n} 次")
        check(f"`kind={kind!r}`：末尾是 AIMessage", tail == "AIMessage", f"末尾是 {tail}")

    print("\n③ 没有回执时不误判（首拍之前 / 普通工具返回）")
    _CALLS.clear()
    _KIND["v"] = "waiting"
    # 最后一条是 HumanMessage（首拍之前）→ 不该跳
    g = create_agent(_FakeModel(), tools=[delegate], system_prompt="x",
                     middleware=[_StopWhenPending()])
    out = g.invoke({"messages": [{"role": "user", "content": "go"}]})
    check("首拍照常发生（`before_model` 在**入口**也跑一次，不能在那儿误跳）",
          len(_CALLS) >= 1 and out["messages"][-1].__class__.__name__ == "ToolMessage",
          f"调用 {len(_CALLS)} 次")

    print("\n④ 🔴 **并行工具调用**（实测踩到的坑）：回执不排在最后也要拦下")
    # ⚠️ **这条是端到端实测翻出来的**（`logs/proto-20261104-174909.log`）：模型把 `delegate`
    #    和 `read_skill` 放在**同一条消息**里 → 待答回执排在这批的**最前** →
    #    只看 `msgs[-1]` 的实现**静默失效**（那次会话 4 个问题只拦下 1 个）。
    #    ⇒ 中间件改成"往前扫末尾连续的那串 ToolMessage"。
    n, tail = _run("waiting", parallel=True)
    check("`delegate` ＋ `read_skill` **并行**：模型仍**只被调 1 次**", n == 1,
          f"被调了 {n} 次 —— 只看最后一条消息的实现会在这里红")
    check("并行那批里**没有主 agent 的正文**", tail == "ToolMessage", f"末尾是 {tail}")

    print("\n⑤ 🔴 **形状二「投递后又问」**：平台投递之后执行器又问了一个（主 agent 手里**没有回执**）")
    # ⚠️ **这条是端到端实测翻出来的后半截**（`logs/proto-20261104-175537.log`）：
    #    投递那条路上**根本没有 `delegate` 调用** —— 主 agent 的消息末尾是用户那句答复，
    #    ④ 那轮"扫工具回执"**扫不到**，于是 4 个问题**只拦下 2 个**
    #    （正好是「本批回执」那两个）。
    #    事实由 `node_prepare` 写在 `AgentState.fed_asking` 上。
    # ⚠️ **用真的 `AgentState`**：这样"那个字段存在"也一起守住了（字段名改了这里就红）。
    from proto.graph import AgentState

    _CALLS.clear()
    _KIND["v"] = "waiting"
    g = create_agent(_FakeModel(), tools=[delegate, read_skill], system_prompt="x",
                     state_schema=AgentState, middleware=[_StopWhenPending()])
    g.invoke({"messages": [{"role": "user", "content": "探究发现型"}], "fed_asking": True})
    check("`fed_asking=True`：模型**一次都没被调**（这一轮主 agent 不发言）",
          len(_CALLS) == 0, f"被调了 {len(_CALLS)} 次")

    # ⚠️ **反面**：用户问的是**别的**（没在答谁）→ `fed_asking=False` → 它必须照常发言。
    #    少了这条，"有待答就禁言"会被误当成正解 —— 那会把用户晾在那儿。
    _CALLS.clear()
    _KIND["v"] = "completed"
    g = create_agent(_FakeModel(), tools=[delegate, read_skill], system_prompt="x",
                     state_schema=AgentState, middleware=[_StopWhenPending()])
    g.invoke({"messages": [{"role": "user", "content": "先别管那个，帮我查个天气"}],
              "fed_asking": False})
    check("`fed_asking=False`（用户问的是别的）→ 主 agent **照常发言**", len(_CALLS) >= 1,
          f"被调了 {len(_CALLS)} 次")

    print("\n⑥ 🔴 **接线**：`build_agent` 必须真的挂上它")
    # ⚠️ 这条断的是"机制**装上了**"，不是"机制**好用**" —— 上面 ①② 断的是好用。
    #    少了它，谁把 `middleware=[...]` 那行删掉都不会有人发现（①② 用的是自己建的 agent）。
    from proto import agent as agent_mod

    _orig = agent_mod.get_model
    agent_mod.get_model = lambda: _FakeModel()  # type: ignore[assignment]
    try:
        built = agent_mod.build_agent(dict)
    finally:
        agent_mod.get_model = _orig  # type: ignore[assignment]
    nodes = set(built.get_graph().nodes)
    check("`build_agent` 的图里有 `_StopWhenPending.before_model` 这个节点",
          f"{_StopWhenPending().name}.before_model" in nodes,
          f"实际节点：{sorted(nodes)}")

    print("\n⑦ 🔴 **`fed_asking` 必须在两个 schema 里都声明**（少一个就静默失效）")
    # ⚠️ **这条是实测翻出来的**（`logs/proto-20261104-181250.log`）：字段**只加进 `AgentState`**
    #    时，`node_prepare` 往父图写的那个键被 **LangGraph 静默丢掉**（不报错、不警告）
    #    → 子图永远收不到 → 「投递后又问」**一次都没生效**（那次 4 个问题只拦下 2 个，
    #    全是「本批回执」那条）。
    #    ⇒ 这就是"**事实在 A 处产生了，B 处看不见**"的又一次翻版（同 `unit_handoff` 那条
    #    "信息在内部产出了，但需要它的地方看不到"）。**两个 schema 都得写。**
    from typing import get_type_hints

    from proto.graph import AgentState, State

    check("`fed_asking` 在 `State` 里（`node_prepare` 要往**父图**写它）",
          "fed_asking" in get_type_hints(State),
          "少了它 → LangGraph 把那个键**悄悄丢掉**，子图收不到")
    check("`fed_asking` 在 `AgentState` 里（中间件要**读**它）",
          "fed_asking" in get_type_hints(AgentState),
          "少了它 → 中间件 `state.get('fed_asking')` 永远是 None")

    # 🔴 **2026-11-06：从"顺手补"改成"按清单补"**（外审那轮冷水泼得对：两轮实锤的 bug
    #    都落在**门禁够不到的接缝**上，而守卫密度在模块内很高、在**边界**上不成比例）。
    #    ⇒ 把「`State` 的每个字段在 `AgentState` 里该不该有」做成**矩阵**：
    #      新加一个字段，**必须先在这张表里表态**（下面的覆盖性断言会红）。
    #    ⚠️ 光看"少声明"是不够的 —— **多声明**同样炸：带 `operator.add` 的字段被子图
    #    原样回吐，父图那份就被又加一遍（实测 `1 → 5 → 12 → 27…` 指数膨胀，
    #    `experiments/probe_double_prepare.py`）。这一半原来**只写在注释里**。
    _FIELD_MATRIX: dict[str, tuple[bool, str]] = {
        # 字段: (该不该在 AgentState 里, 为什么)
        "messages": (True, "子图有自己的对话（add_messages，幂等）"),
        "tasks": (True, "子图要读台账（`_merge_tasks` 按 id 合，幂等）"),
        "fed_asking": (True, "`_StopWhenPending` 要**读**它 —— 本轮信号"),
        "log": (False, "`operator.add` —— 子图回吐会让父图**每轮翻倍**"),
        "actions": (False, "同上（`operator.add`）"),
        "answer_to": (False, "子图不回吐，`feed` 才清得掉；它归 `feed` 每轮清"),
    }
    _s_fields, _a_fields = set(get_type_hints(State)), set(get_type_hints(AgentState))
    check("矩阵**覆盖 `State` 的每一个字段**（新加字段必须在这里表态，不许靠顺手）",
          set(_FIELD_MATRIX) == _s_fields,
          f"没表态的：{sorted(_s_fields - set(_FIELD_MATRIX))}")
    _wrong = [f"{f}（该{'在' if want else '不在'} AgentState）"
              for f, (want, _) in _FIELD_MATRIX.items() if (f in _a_fields) != want]
    check("每个字段在 `AgentState` 里的**在场/缺席**都与矩阵一致", not _wrong, repr(_wrong))
    check("`AgentState` 里没有矩阵之外的字段（多一个也是没表态）",
          _a_fields <= set(_FIELD_MATRIX), repr(sorted(_a_fields - set(_FIELD_MATRIX))))

    print("\n⑧ 🔴 **穷举证明的前提**：落终态只有那一条路（`dispatch`），它只有那两个调用点")
    # 这个机制的覆盖面是**证出来的**，不是跑出来的（见 `_StopWhenPending` 的 docstring）：
    #   一个 Task 进入 `waiting_for_input` 只有两条路 —— 因为 `settle()` 是**唯一**写终态
    #   的地方（`Task.stage` 全仓只有 `superseded` / `running` / `settle` 三个写点），
    #   而 `settle()` 只在 `agent.dispatch` 里被调用，`dispatch()` 又只被
    #   `delegate` 和 `_feed_waiting` 调用。
    # ⚠️ **多一个调用位置 = 多一条产生待答的路 = 那个证明当场失效** ——
    #    而且新路**不会被 `_StopWhenPending` 覆盖**（它只认那两个形状）→ 静默退回"复述"。
    #    ⇒ 这条必须**会红**，不能靠记得。
    # 🔴 **2026-11-06 改（外审 F5 收口）**：`settle()` 原来在 `delegate` / `_feed_waiting`
    #    里各调一次 —— 两条路的**顺序**于是各写一遍，实测漂过一次（投递那条路的试答重跑
    #    拿到的是上一轮的 report，见 `tmp/probe_feed_rerun_briefing.py`）。现在收进
    #    `agent.dispatch`。**证明的形状没变，只是多了一跳**：所以这里要**两段一起**断 ——
    #    `settle` 只许在 `dispatch` 里，`dispatch` 只许被那两条路调。
    import ast
    from pathlib import Path

    def _call_sites(fname: str) -> set[tuple[str, str]]:
        """全仓扫 `fname(...)` 的调用，返回 {(文件, 所在函数)}。"""
        root = Path(__file__).resolve().parent.parent / "proto"
        found: set[tuple[str, str]] = set()
        for py in root.rglob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            fns = [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for fn in fns:
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id == fname):
                        rel = py.relative_to(root.parent).as_posix()
                        found.add((rel, fn.name))
        return found

    _settle_expected = {("proto/agent.py", "dispatch")}
    _sites = _call_sites("settle")
    check("`settle()` 的调用位置**只有** `agent.dispatch`（写终态的唯一入口）",
          _sites == _settle_expected,
          f"实际：{sorted(_sites)} —— 多一条就说明**多了一条产生待答的路**，"
          f"而 `_StopWhenPending` **不认**它（覆盖面的证明失效，会静默退回复述）")
    _dispatch_expected = {("proto/agent.py", "delegate"), ("proto/graph.py", "_feed_waiting")}
    _d_sites = _call_sites("dispatch")
    check("`dispatch()` 的调用位置**只有** `delegate` 和 `_feed_waiting`",
          _d_sites == _dispatch_expected,
          f"实际：{sorted(_d_sites)} —— 同上：多一条就是多一条产生待答的路")

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败：")
        for f in FAIL:
            print(f"  ❌ {f}")
    print("═" * 78)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
