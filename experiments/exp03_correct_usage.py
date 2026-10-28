"""实验 03：正确用法能不能端到端跑通？

实验 02 的结论：
  - dispatch 包装**不传** config  → interrupt 冒泡到顶层 ✅
  - dispatch 包装**传** thread config → 子 agent 变"独立根图"，interrupt 被隔离 ❌

本实验验证两条修法：
  A. 不传 config（最小改动）—— 端到端：挂起 → 顶层拿到 → resume → 子 agent 继续完成
  B. 包装层主动上抛 —— 检测到 __interrupt__ 后，在包装里再调一次 interrupt() 抛给顶层
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
from langgraph.types import Command, interrupt


class ToolFakeChatModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self


def tc(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def build_subagent():
    """子 agent：请求用户输入的函数内部调用 interrupt()。per-invocation（不设 checkpointer）。"""

    @tool
    def ask_user(question: str) -> str:
        """向用户提问并等待回答。"""
        answer = interrupt({"question": question})
        return f"用户回答：{answer}"

    return create_agent(
        ToolFakeChatModel(
            responses=[
                tc("ask_user", {"question": "请提供年级/学科"}, "s1"),
                AIMessage(content="收到参数，继续执行。"),
            ]
        ),
        tools=[ask_user],
        system_prompt="你是备课助手。缺参数时调用 ask_user。",
        name="beike_agent",
    )


def parent_model() -> ToolFakeChatModel:
    return ToolFakeChatModel(
        responses=[
            tc("run_skill", {"request": "帮我备课"}, "p1"),
            AIMessage(content="任务完成。"),
        ]
    )


def build_parent_A(subagent):
    """修法 A：包装不传 config。"""

    @tool
    def run_skill(request: str) -> str:
        """把任务交给子 agent 执行。"""
        resp = subagent.invoke({"messages": [{"role": "user", "content": request}]})
        return resp["messages"][-1].content

    return create_agent(
        parent_model(), tools=[run_skill], system_prompt="把任务交给 run_skill。",
        checkpointer=InMemorySaver(), name="parent_A",
    )


def build_parent_B(subagent):
    """修法 B：包装检测 __interrupt__ 并主动上抛给顶层。"""

    @tool
    def run_skill(request: str) -> str:
        """把任务交给子 agent 执行。"""
        resp = subagent.invoke({"messages": [{"role": "user", "content": request}]})
        pending = resp.get("__interrupt__")
        if pending:
            # 把子 agent 的待问请求上抛给顶层，由编排层统一提问
            questions = [p.value for p in pending]
            answers = interrupt({"from": "run_skill", "questions": questions})
            return f"【上抛后拿到答案】{answers}"
        return resp["messages"][-1].content

    return create_agent(
        parent_model(), tools=[run_skill], system_prompt="把任务交给 run_skill。",
        checkpointer=InMemorySaver(), name="parent_B",
    )


def banner(t: str) -> None:
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def case_a() -> None:
    banner("修法 A：包装不传 config —— 端到端挂起 → 恢复")
    parent = build_parent_A(build_subagent())
    config = {"configurable": {"thread_id": "tA"}}

    r1 = parent.invoke({"messages": [{"role": "user", "content": "帮我备课"}]}, config)
    pend = r1.get("__interrupt__")
    print(f"  ① 首次运行 __interrupt__ = {[(p.value) for p in pend] if pend else None}")
    if not pend:
        print("  ❌ 没挂起，后续无从验证")
        return

    r2 = parent.invoke(Command(resume="三年级 / 数学"), config)
    pend2 = r2.get("__interrupt__")
    print(f"  ② resume 后 __interrupt__ = {[p.value for p in pend2] if pend2 else None}")
    print(f"  ③ 最后一条消息 = {r2['messages'][-1].content!r}")
    print(f"  → {'✅ 端到端跑通' if not pend2 else '❌ 又挂起了（未收敛）'}")


def case_b() -> None:
    banner("修法 B：包装层主动上抛")
    parent = build_parent_B(build_subagent())
    config = {"configurable": {"thread_id": "tB"}}

    r1 = parent.invoke({"messages": [{"role": "user", "content": "帮我备课"}]}, config)
    pend = r1.get("__interrupt__")
    print(f"  ① 首次运行 __interrupt__ = {[p.value for p in pend] if pend else None}")
    if not pend:
        print("  ❌ 没挂起")
        return

    r2 = parent.invoke(Command(resume={"请提供年级/学科": "三年级 / 数学"}), config)
    pend2 = r2.get("__interrupt__")
    print(f"  ② resume 后 __interrupt__ = {[p.value for p in pend2] if pend2 else None}")
    print(f"  ③ 最后一条消息 = {r2['messages'][-1].content!r}")
    print(f"  → {'✅ 上抛成功' if not pend2 else '❌ 又挂起了'}")


if __name__ == "__main__":
    case_a()
    case_b()
    print()
