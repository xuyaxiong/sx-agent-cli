"""实验 02：为什么 interrupt 会被吞掉？——把 dispatch 包装内部看穿。

实验 01 发现：dispatch 包装若把 config 显式传给 subagent.invoke(...)，
interrupt 就传不到顶层；而 per-thread + 传 config 会直接 RuntimeError。

本实验细化：
  1. run_skill 内部到底拿到了什么？（resp["__interrupt__"] / messages[-1]）
  2. 被吞掉时，subagent 自己是真挂起了，还是压根没挂起？
  3. 包装层能不能检测到并向上传播？
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
    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self


def tc(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def build_subagent(checkpointer):
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
        checkpointer=checkpointer,
        name="beike_agent",
    )


def build_parent(subagent, config_mode: str, seen: list):
    @tool
    def run_skill(request: str) -> str:
        """把任务交给子 agent 执行。"""
        if config_mode == "thread":
            cfg = {"configurable": {"thread_id": SUB_THREAD}}
        elif config_mode == "empty":
            cfg = {}
        else:
            cfg = None

        resp = subagent.invoke({"messages": [{"role": "user", "content": request}]}, cfg)

        last = resp["messages"][-1]
        seen.append(
            {
                "has_interrupt": bool(resp.get("__interrupt__")),
                "interrupt": resp.get("__interrupt__"),
                "last_type": type(last).__name__,
                "last_content": str(last.content)[:60],
                "last_tool_calls": getattr(last, "tool_calls", None),
            }
        )
        return last.content  # ← 被怀疑丢掉 interrupt 的那一行

    return create_agent(
        ToolFakeChatModel(
            responses=[
                tc("run_skill", {"request": "帮我备课"}, "p1"),
                AIMessage(content="任务完成。"),
            ]
        ),
        tools=[run_skill],
        system_prompt="把任务交给 run_skill。",
        checkpointer=InMemorySaver(),
        name="parent_agent",
    )


def run_case(*, mode: str, config_mode: str) -> None:
    checkpointer = None if mode == "per-invocation" else True
    subagent = build_subagent(checkpointer)
    seen: list = []
    parent = build_parent(subagent, config_mode, seen)

    config = {"configurable": {"thread_id": "main-thread-1"}}
    print(f"\n--- mode={mode:<15} config_mode={config_mode:<8} ---")
    try:
        result = parent.invoke({"messages": [{"role": "user", "content": "帮我备课"}]}, config)
    except Exception as e:  # noqa: BLE001
        print(f"  顶层抛异常: {type(e).__name__}: {e}")
        return

    if seen:
        s = seen[0]
        print(f"  run_skill 内部: __interrupt__={s['has_interrupt']}  last={s['last_type']}({s['last_content']!r})")
        if s["last_tool_calls"]:
            print(f"                   last.tool_calls={s['last_tool_calls']}")
    print(f"  顶层 __interrupt__: {'有 ✅' if result.get('__interrupt__') else '无 ❌'}")

    # 子 agent 自己是否处于挂起状态？
    try:
        st = subagent.get_state({"configurable": {"thread_id": SUB_THREAD}})
        nxt = tuple(st.next) if st and st.next else ()
        print(f"  子 agent 自身状态: next={nxt}  (非空 => 它自己确实挂起了)")
    except Exception as e:  # noqa: BLE001
        print(f"  子 agent 状态查询失败: {type(e).__name__}: {e}")


if __name__ == "__main__":
    print("=" * 72)
    print("实验 02：interrupt 在哪一层消失")
    print("=" * 72)
    for mode in ("per-invocation", "per-thread"):
        for config_mode in ("none", "empty", "thread"):
            run_case(mode=mode, config_mode=config_mode)
    print()
