"""实验 05：P1 的批量方案 —— 两个 subagent 同时挂起，一次问完

复现用户场景：用户在一轮里选了【备课】+【导学单】两个 skill，
两个 subagent 都要"年级/学科"这类相似参数。

验证：
  1. 两个 subagent 的 interrupt 能否同时浮到顶层？（对应 stream.interrupts）
  2. 能否用一次 Command(resume={interrupt_id: value}) 批量恢复？
  3. 编排层能不能在恢复前做"合并去重"？
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


def build_skill_agent(name: str, fields: list[str], id_prefix: str):
    """一个 skill = 一个 subagent。缺参数时调用 ask_user（内部 interrupt）。"""

    @tool
    def ask_user(question: str) -> str:
        """向用户提问并等待回答。"""
        answer = interrupt({"skill": name, "question": question, "fields": fields})
        return f"用户回答：{answer}"

    return create_agent(
        ToolFakeChatModel(
            responses=[
                tc("ask_user", {"question": f"[{name}] 请提供 {'/'.join(fields)}"}, f"{id_prefix}1"),
                AIMessage(content=f"[{name}] 已完成。"),
            ]
        ),
        tools=[ask_user],
        system_prompt=f"你是{name}助手。缺参数时调用 ask_user。",
        name=name,
    )


def build_parent(beike, daoxuedan):
    @tool
    def run_beike(request: str) -> str:
        """执行备课 skill。"""
        return beike.invoke({"messages": [{"role": "user", "content": request}]})["messages"][-1].content

    @tool
    def run_daoxuedan(request: str) -> str:
        """执行导学单 skill。"""
        return daoxuedan.invoke({"messages": [{"role": "user", "content": request}]})["messages"][-1].content

    return create_agent(
        ToolFakeChatModel(
            responses=[
                # 一次调用两个 skill（并行）
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "run_beike", "args": {"request": "备课"}, "id": "b1"},
                        {"name": "run_daoxuedan", "args": {"request": "导学单"}, "id": "d1"},
                    ],
                ),
                AIMessage(content="两个 skill 都完成了。"),
            ]
        ),
        tools=[run_beike, run_daoxuedan],
        system_prompt="按需调用 run_beike / run_daoxuedan。",
        checkpointer=InMemorySaver(),
        name="parent",
    )


def banner(t: str) -> None:
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


if __name__ == "__main__":
    banner("实验 05：两个 skill 同时索要相似参数 → 批量去重 → 一次恢复")

    beike = build_skill_agent("备课", ["年级", "学科", "课题"], "bk")
    daoxuedan = build_skill_agent("导学单", ["年级", "学科", "课时"], "dx")
    parent = build_parent(beike, daoxuedan)
    config = {"configurable": {"thread_id": "p1-demo"}}

    r1 = parent.invoke({"messages": [{"role": "user", "content": "帮我备课并出导学单"}]}, config)
    pending = r1.get("__interrupt__") or []
    print(f"\n① 顶层收到 {len(pending)} 个待问请求：")
    for p in pending:
        print(f"   id={p.id[:12]}…  {p.value}")

    if len(pending) < 2:
        print("\n❌ 两个 subagent 的 interrupt 没能同时浮到顶层")
        raise SystemExit(1)

    # ---- 编排层的合并去重（这里演示"同名字段只问一次"）----
    print("\n② 编排层合并去重：")
    merged: dict[str, str] = {}
    for p in pending:
        for f in p.value["fields"]:
            merged.setdefault(f, "")
    print(f"   去重后的字段并集 = {list(merged)}")
    print(f"   （若不合并，用户会被问 {sum(len(p.value['fields']) for p in pending)} 个字段）")

    # 假装用户一次性回答了所有字段
    user_answers = {"年级": "三年级", "学科": "数学", "课题": "分数的初步认识", "课时": "1"}
    resume_map = {}
    for p in pending:
        vals = {f: user_answers[f] for f in p.value["fields"]}
        resume_map[p.id] = vals
    print(f"\n③ 一次性批量恢复 {len(resume_map)} 个 interrupt（Command(resume={{id: value}})）")

    r2 = parent.invoke(Command(resume=resume_map), config)
    left = r2.get("__interrupt__")
    print(f"\n④ 恢复后剩余 interrupt = {left}")
    print(f"   最后一条消息 = {r2['messages'][-1].content!r}")
    print(f"   → {'✅ 批量恢复成功，两个 skill 都继续跑完了' if not left else '❌ 仍有挂起'}")
    print()
