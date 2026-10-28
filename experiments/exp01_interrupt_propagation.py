"""实验 01：subagent 内部的 interrupt() 能否穿透 tool 包装到达顶层图？

复现用户 P3 的核心问题：
  subagent 请求用户输入时**调用了 interrupt()**，但"形式上似乎向外请求参数，
  其实已经正常退出了"。

对照两个维度：
  1. 子图持久化模式：per-invocation(None) vs per-thread(True)
  2. dispatch 包装是否显式把 config 传给 subagent

全部使用 fake chat model，不需要 API key，结果确定。
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
from langgraph.types import interrupt

SUB_THREAD = "sub-thread-1"


class ToolFakeChatModel(FakeMessagesListChatModel):
    """FakeMessagesListChatModel 没实现 bind_tools，而 create_agent 会调用它。"""

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self


def tc(name: str, args: dict, call_id: str) -> AIMessage:
    """构造一个只含 tool_call 的 AI 消息。"""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def sub_model() -> ToolFakeChatModel:
    """子 agent 的假模型：先调 ask_user，拿到回答后收尾。"""
    return ToolFakeChatModel(
        responses=[
            tc("ask_user", {"question": "请提供年级/学科"}, "s1"),
            AIMessage(content="收到参数，继续执行。"),
        ]
    )


def parent_model() -> ToolFakeChatModel:
    """父 agent 的假模型：先调 run_skill，拿到结果后收尾。"""
    return ToolFakeChatModel(
        responses=[
            tc("run_skill", {"request": "帮我备课"}, "p1"),
            AIMessage(content="任务完成。"),
        ]
    )


def build_subagent(checkpointer):
    """子 agent：请求用户输入的函数内部调用 interrupt()。"""

    @tool
    def ask_user(question: str) -> str:
        """向用户提问并等待回答。"""
        answer = interrupt({"question": question})  # ← 关键：函数内调用 interrupt
        return f"用户回答：{answer}"

    return create_agent(
        sub_model(),
        tools=[ask_user],
        system_prompt="你是备课助手。缺参数时调用 ask_user。",
        checkpointer=checkpointer,
        name="beike_agent",
    )


def build_parent(subagent, *, pass_config: bool):
    """父 agent：通过一个 tool 包装来调用 subagent（文档里的 subagents 模式）。"""

    @tool
    def run_skill(request: str) -> str:
        """把任务交给子 agent 执行。"""
        cfg = {"configurable": {"thread_id": SUB_THREAD}} if pass_config else None
        resp = subagent.invoke(
            {"messages": [{"role": "user", "content": request}]},
            cfg,
        )
        # ← 这就是被怀疑"丢掉 interrupt"的那一行
        return resp["messages"][-1].content

    return create_agent(
        parent_model(),
        tools=[run_skill],
        system_prompt="把任务交给 run_skill。",
        checkpointer=InMemorySaver(),
        name="parent_agent",
    )


def run_case(*, mode: str, pass_config: bool) -> None:
    checkpointer = None if mode == "per-invocation" else True
    subagent = build_subagent(checkpointer)
    parent = build_parent(subagent, pass_config=pass_config)

    config = {"configurable": {"thread_id": "main-thread-1"}}
    print(f"\n--- mode={mode:<15} pass_config={pass_config} ---")
    try:
        result = parent.invoke(
            {"messages": [{"role": "user", "content": "帮我备课"}]}, config
        )
    except Exception as e:  # noqa: BLE001
        print(f"  抛异常: {type(e).__name__}: {e}")
        return

    pending = result.get("__interrupt__")
    print(f"  顶层 result['__interrupt__'] = {pending!r}")
    if pending:
        print("  ✅ interrupt 穿透到顶层 —— 挂起信号可见")
    else:
        print("  ❌ 顶层看不到 interrupt —— 信号被吞掉了")
    last = result["messages"][-1]
    print(f"  最后一条消息: {type(last).__name__} / {str(last.content)[:80]!r}")


if __name__ == "__main__":
    print("=" * 72)
    print("实验 01：subagent 内部 interrupt() 的穿透性")
    print("=" * 72)
    for mode in ("per-invocation", "per-thread"):
        for pass_config in (False, True):
            run_case(mode=mode, pass_config=pass_config)
    print()
