"""实验 04：复刻文档里的 create_sub_agent 模式（最接近用户现场）

用户说用的是 "create subagent 这种机制" + checkpointer=True。
文档 use-subgraphs.md:613 的模式是：

    agent = create_agent(model=..., name=name, checkpointer=True, ...)
    return StateGraph(MessagesState).add_node(name, agent).add_edge("__start__", name).compile()

注意两层：
  - 内层 create_agent 拿到 checkpointer=True
  - 外层 StateGraph.compile() 不带 checkpointer → 继承父图

本实验对比：
  A. create_sub_agent 包装，作为 tool 调用（不传 config）
  B. create_sub_agent 包装，作为 tool 调用（传 thread config）
  C. create_sub_agent 包装，作为【父图节点】add_node
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.types import Command, interrupt


class ToolFakeChatModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self


def tc(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def sub_model() -> ToolFakeChatModel:
    return ToolFakeChatModel(
        responses=[
            tc("ask_user", {"question": "请提供年级/学科"}, "s1"),
            AIMessage(content="收到参数，继续执行。"),
        ]
    )


def parent_model() -> ToolFakeChatModel:
    return ToolFakeChatModel(
        responses=[
            tc("run_skill", {"request": "帮我备课"}, "p1"),
            AIMessage(content="任务完成。"),
        ]
    )


def create_sub_agent(*, name: str, checkpointer):
    """文档 use-subgraphs.md:613 的模式，逐字复刻。"""

    @tool
    def ask_user(question: str) -> str:
        """向用户提问并等待回答。"""
        answer = interrupt({"question": question})
        return f"用户回答：{answer}"

    agent = create_agent(
        sub_model(), tools=[ask_user], system_prompt="缺参数时调用 ask_user。",
        name=name, checkpointer=checkpointer,
    )
    return (
        StateGraph(MessagesState)
        .add_node(name, agent)  # unique name → stable namespace
        .add_edge(START, name)
        .compile()
    )


def banner(t: str) -> None:
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def case_tool(*, checkpointer, pass_config: bool) -> None:
    sub = create_sub_agent(name="beike_agent", checkpointer=checkpointer)

    @tool
    def run_skill(request: str) -> str:
        """把任务交给子 agent。"""
        cfg = {"configurable": {"thread_id": "sub-t"}} if pass_config else None
        resp = sub.invoke({"messages": [{"role": "user", "content": request}]}, cfg)
        return resp["messages"][-1].content

    parent = create_agent(
        parent_model(), tools=[run_skill], system_prompt="交给 run_skill。",
        checkpointer=InMemorySaver(), name="parent",
    )
    cfg = {"configurable": {"thread_id": "main-t"}}
    tag = f"checkpointer={checkpointer!s:<5} pass_config={pass_config!s:<5}"
    try:
        r1 = parent.invoke({"messages": [{"role": "user", "content": "帮我备课"}]}, cfg)
    except Exception as e:  # noqa: BLE001
        print(f"  [tool] {tag} → 抛异常 {type(e).__name__}: {e}")
        return

    pend = r1.get("__interrupt__")
    if not pend:
        print(f"  [tool] {tag} → 顶层无 interrupt ❌")
        return
    print(f"  [tool] {tag} → 顶层有 interrupt ✅ {[p.value for p in pend]}")
    r2 = parent.invoke(Command(resume="三年级/数学"), cfg)
    ok = not r2.get("__interrupt__")
    print(f"          resume → {'✅ 收敛，末条=' + repr(r2['messages'][-1].content)}")


def case_node(*, checkpointer) -> None:
    """把 subagent 作为父图的节点（而非 tool）。"""
    sub = create_sub_agent(name="beike_agent", checkpointer=checkpointer)

    def dispatch(state: MessagesState):
        return sub.invoke({"messages": [{"role": "user", "content": "帮我备课"}]})

    parent = (
        StateGraph(MessagesState)
        .add_node("dispatch", dispatch)
        .add_edge(START, "dispatch")
        .compile(checkpointer=InMemorySaver())
    )
    cfg = {"configurable": {"thread_id": "main-node-t"}}
    try:
        r1 = parent.invoke({"messages": [{"role": "user", "content": "帮我备课"}]}, cfg)
    except Exception as e:  # noqa: BLE001
        print(f"  [node] checkpointer={checkpointer!s:<5} → 抛异常 {type(e).__name__}: {e}")
        return
    pend = r1.get("__interrupt__")
    print(f"  [node] checkpointer={checkpointer!s:<5} → {'有 ✅ ' + str([p.value for p in pend]) if pend else '无 ❌'}")


if __name__ == "__main__":
    banner("A/B. create_sub_agent 作为 tool 调用")
    for cp in (None, True):
        for pc in (False, True):
            case_tool(checkpointer=cp, pass_config=pc)

    banner("C. create_sub_agent 作为父图节点 add_node")
    for cp in (None, True):
        case_node(checkpointer=cp)
    print()
