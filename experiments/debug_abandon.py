"""诊断：interrupt 挂起后，用户**不回答、直接换话题**会怎样？

这是 sx-agent 当初关掉 interrupt 的原因之一
（docs/05_AGENT_SKILL_FLOW.md:1071）：
  "悬空 tool_call（无配对 ToolMessage）会在后续工具循环中重新进入 LLM 输入，
   触发上游 API 400（insufficient tool messages following tool_calls）"

本脚本验证：把「提问」做成**图节点**（而不是工具）时，还会不会有悬空 tool_call。
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from proto.config import describe
from proto.graph import build


def dangling_tool_calls(messages: list) -> list[str]:
    """找出没有配对 ToolMessage 的 tool_call。"""
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    out = []
    for m in messages:
        if isinstance(m, AIMessage):
            for c in getattr(m, "tool_calls", None) or []:
                if c.get("id") not in answered:
                    out.append(f"{c.get('name')}#{c.get('id')}")
    return out


def show(tag: str, result: dict) -> None:
    msgs = result.get("messages") or []
    dangle = dangling_tool_calls(msgs)
    kinds = [type(m).__name__.replace("Message", "") for m in msgs]
    print(f"\n  [{tag}] 消息序列: {' → '.join(kinds)}")
    print(f"         悬空 tool_call: {dangle or '无 ✅'}")
    print(f"         awaiting: {result.get('awaiting')}")


if __name__ == "__main__":
    print(f"模型：{describe()}")
    graph = build("interrupt")
    cfg = {"configurable": {"thread_id": "abandon-test", "proto_mode": "interrupt"}}

    print("\n① 发起请求 → 应挂起")
    r1 = graph.invoke({"messages": [HumanMessage("帮我出随堂小测，三年级数学")], "log": []}, cfg)
    pend = r1.get("__interrupt__")
    print(f"   __interrupt__ = {[p.value['fields'] for p in pend] if pend else None}")
    show("挂起后", r1)

    print("\n② 用户**不回答**，直接换话题（发一条全新消息）")
    r2 = graph.invoke(
        {"messages": [HumanMessage("算了，先帮我备一节课，四年级语文")], "log": []}, cfg
    )
    show("换话题后", r2)
    print(f"   本轮 __interrupt__ = {[p.value['fields'] for p in r2.get('__interrupt__') or []]}")

    print("\n③ 再正常回答一次，看还能不能收敛")
    pend2 = r2.get("__interrupt__")
    if pend2:
        r3 = graph.invoke(Command(resume="课题=观潮 课时=1"), cfg)
        show("回答后", r3)
    else:
        print("   （本轮没挂起，无需回答）")

    print("\n" + "─" * 70)
    all_msgs = (r2.get("messages") or [])
    d = dangling_tool_calls(all_msgs)
    print(f"  结论：{'✅ 全程无悬空 tool_call' if not d else f'❌ 出现悬空: {d}'}")
    print("  （提问做成图节点时，interrupt() 不在 tool_call 里，天然没有配对问题）")
    print()
