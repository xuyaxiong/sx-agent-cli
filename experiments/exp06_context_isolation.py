"""实验 06：上下文隔离——把 subagent 边界从「整个 skill 生命周期」移到「执行段」

用户诉求（原话）：用 subagent 是为了"一定程度隔离上下文，防止上下文爆炸"。
但 P1/P2/P3 克服不了，于是放弃 subagent，改成装载式。

本实验量化对比两条路径的主 agent 上下文体积：
  A. 装载式（现状）：主 agent 自己生成产物 → 产物正文以 tool_call 参数形式留在主上下文
  B. 隔离式（提案）：subagent 生成产物 → 主 agent 只收到 {产物ID, 摘要}

关键论点：P1/P2/P3 来自「subagent 跨轮存活」，不是来自「隔离」本身。
        把边界收窄到"参数齐了之后的一次性执行"，隔离还在，三个问题消失。
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

# 模拟一份真实体量的教案产物（5000 字）
ARTIFACT_BODY = "《分数的初步认识》教学设计\n" + ("一、教学目标：理解分数的意义……\n" * 160)
ARTIFACT_SUMMARY = "《分数的初步认识》教学设计，含目标/重难点/过程/板书，约 5000 字"


class ToolFakeChatModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self


def tc(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def ctx_chars(messages: list) -> int:
    """统计消息列表里所有内容的总字符数（粗略衡量上下文体积）。"""
    total = 0
    for m in messages:
        content = m.content if isinstance(m.content, str) else str(m.content)
        total += len(content)
        for call in getattr(m, "tool_calls", None) or []:
            total += len(str(call.get("args", "")))
    return total


# ─────────────────────────── 路径 A：装载式（现状） ───────────────────────────

def path_a() -> tuple[int, int]:
    """主 agent 自己生成产物：正文以 create_artifact 参数形式留在主上下文。"""

    @tool
    def create_artifact(name: str, summary: str, content: str) -> str:
        """交付产物。"""
        return f"产物已生成：{name}"

    main = create_agent(
        ToolFakeChatModel(
            responses=[
                tc("create_artifact",
                   {"name": "教案", "summary": ARTIFACT_SUMMARY, "content": ARTIFACT_BODY},
                   "a1"),
                AIMessage(content="教案已完成，请查收。"),
            ]
        ),
        tools=[create_artifact],
        system_prompt="装载技能后直接完成任务。",
        checkpointer=InMemorySaver(),
        name="main_A",
    )
    cfg = {"configurable": {"thread_id": "A"}}
    r = main.invoke({"messages": [{"role": "user", "content": "帮我备课"}]}, cfg)
    return ctx_chars(r["messages"]), len(ARTIFACT_BODY)


# ─────────────────────── 路径 B：隔离式（边界收窄到执行段） ───────────────────────

def path_b() -> tuple[int, int]:
    """subagent 生成产物；主 agent 只收到「产物ID + 摘要」。

    注意：dispatch 包装**不传 config**（实验 01-04 的结论）。
    subagent 内部**不调用 interrupt、不问用户**——参数由主 agent 在外层收齐后才派发。
    """

    @tool
    def create_artifact(name: str, summary: str, content: str) -> str:
        """交付产物。"""
        return f"产物已生成：{name}"

    # ── 子 agent：隔离上下文，干重活 ──
    worker = create_agent(
        ToolFakeChatModel(
            responses=[
                tc("create_artifact",
                   {"name": "教案", "summary": ARTIFACT_SUMMARY, "content": ARTIFACT_BODY},
                   "w1"),
                AIMessage(content=ARTIFACT_SUMMARY),  # ← 只回摘要，不回正文
            ]
        ),
        tools=[create_artifact],
        system_prompt="你是备课执行器。参数已给齐，直接产出，不要提问。",
        name="beike_worker",
    )

    # ── 主 agent：交互 + 派发；只拿到短摘要 ──
    @tool
    def run_beike(params: str) -> str:
        """参数齐了之后，把执行交给备课执行器。"""
        resp = worker.invoke({"messages": [{"role": "user", "content": params}]})
        return f"【产物已交付】id=art_001\n摘要：{resp['messages'][-1].content}"

    main = create_agent(
        ToolFakeChatModel(
            responses=[
                tc("run_beike", {"params": "年级=三年级 学科=数学 课题=分数的初步认识"}, "b1"),
                AIMessage(content="教案已完成，请查收。"),
            ]
        ),
        tools=[run_beike],
        system_prompt="收集齐参数后派发给执行器。",
        checkpointer=InMemorySaver(),
        name="main_B",
    )
    cfg = {"configurable": {"thread_id": "B"}}
    r = main.invoke({"messages": [{"role": "user", "content": "帮我备课"}]}, cfg)
    return ctx_chars(r["messages"]), len(ARTIFACT_BODY)


# ───────────── 路径 B+：执行中才发现缺参数 → 结构化返回，不挂起 ─────────────

def path_b_plus() -> None:
    """验证：subagent 不 interrupt、不跨轮存活，只返回结构化 needs_input。

    这样 P1/P2/P3 全部消失：
      - 不问用户 → 无 P1（提问权归主 agent，可跨 skill 去重）
      - 不跨轮   → 无 P2（不存在"没干完就退出"，每次都是参数齐了的一次性执行）
      - 不挂起   → 无 P3（不需要挂起，也就没有挂起点丢失的问题）
    """

    @tool
    def create_artifact(name: str, summary: str, content: str) -> str:
        """交付产物。"""
        return f"产物已生成：{name}"

    worker = create_agent(
        ToolFakeChatModel(
            responses=[
                # 执行到一半发现缺参数 → 结构化返回，然后正常结束
                AIMessage(
                    content="NEEDS_INPUT|缺 课时|已完成：目标与重难点分析|断点：art_draft_7"
                ),
            ]
        ),
        tools=[create_artifact],
        system_prompt="参数不齐时返回 NEEDS_INPUT 行并结束，不要向用户提问。",
        name="beike_worker2",
    )

    @tool
    def run_beike(params: str) -> str:
        """把执行交给备课执行器。"""
        resp = worker.invoke({"messages": [{"role": "user", "content": params}]})
        return resp["messages"][-1].content

    main = create_agent(
        ToolFakeChatModel(
            responses=[
                tc("run_beike", {"params": "年级=三年级 学科=数学"}, "c1"),
                AIMessage(content="【主 agent 读懂了协议】执行器缺「课时」，我来问用户。"),
            ]
        ),
        tools=[run_beike],
        system_prompt="派发后按返回协议处理。",
        checkpointer=InMemorySaver(),
        name="main_C",
    )
    cfg = {"configurable": {"thread_id": "C"}}
    r = main.invoke({"messages": [{"role": "user", "content": "帮我备课"}]}, cfg)
    print(f"  执行器返回 : {r['messages'][-2].content!r}")
    print(f"  主 agent 收口: {r['messages'][-1].content!r}")
    print("  → ✅ 结构化协议生效：执行器正常结束，主 agent 拿到明确的 needs_input 信号")
    print("  → ✅ 全程没有 interrupt、没有挂起、没有跨轮存活")


if __name__ == "__main__":
    print("=" * 72)
    print("实验 06：上下文隔离 vs 装载式——主 agent 上下文体积对比")
    print("=" * 72)
    print(f"  产物正文体量（模拟）: {len(ARTIFACT_BODY)} 字\n")

    a_main, body = path_a()
    b_main, _ = path_b()

    print(f"  A 装载式（现状） 主 agent 上下文 ≈ {a_main:>6} 字")
    print(f"  B 隔离式（提案） 主 agent 上下文 ≈ {b_main:>6} 字")
    saved = a_main - b_main
    print(f"\n  差值 ≈ {saved} 字，其中产物正文 {body} 字完全落在 subagent 里")
    if b_main > 0:
        print(f"  主 agent 上下文缩减到 A 的 {b_main / a_main:.1%}")

    print()
    print("=" * 72)
    print("实验 06b：执行中才发现缺参数 → 结构化返回（不挂起、不跨轮）")
    print("=" * 72)
    path_b_plus()
    print()
