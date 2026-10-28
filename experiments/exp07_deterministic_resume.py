"""实验 07：用户答复能不能保证传回正确的执行器？

用户的质疑（原话）："主 agent 获取用户输入后能保证一定传回正确的 subagent 吗"

答案：**不能靠 LLM 保证，但可以让 LLM 根本没有机会选错。**

做法：把「待办任务」变成 state 里的一等公民（pending_task），
      入口路由与续跑路由**全部由代码判定**，不经过 LLM 决策。

本实验用一个**故意"困惑"的主 agent 模型**来证伪 LLM 路径：
该模型若被调用，会派发到**错误的**技能（lesson-prep）；
但续跑路径根本不经过它，所以正确的执行器（guided-learning）照样拿到答复。

—— 这就是「LLM 负责启动，代码负责续跑」。
"""

from __future__ import annotations

import sys
from typing import Annotated, NotRequired, TypedDict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

# 记录每个执行器被调用的次数，用来证明"传给了正确的那个"
CALLS: dict[str, int] = {"lesson-prep": 0, "guided-learning": 0}


class ToolFakeChatModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self


def tc(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def make_executor(skill: str, scripted_reply: str):
    """执行器 = 一个 create_agent 子图。隔离上下文，只回结构化结果，不问用户。"""

    def run(params: str) -> str:
        CALLS[skill] += 1
        agent = create_agent(
            ToolFakeChatModel(responses=[AIMessage(content=scripted_reply)]),
            tools=[],
            system_prompt=f"你是{skill}执行器。",
            name=f"exec_{skill}",
        )
        # 注意：不传 config（实验 01-04 的结论）
        resp = agent.invoke({"messages": [{"role": "user", "content": params}]})
        return resp["messages"][-1].content

    return run


#: 执行器注册表：**按名字查表**，不是 LLM 选择
EXECUTORS = {
    "lesson-prep": make_executor("lesson-prep", "【备课】已完成"),
    "guided-learning": make_executor("guided-learning", "NEEDS_INPUT|缺 课时|断点：dx_draft_3"),
}


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    pending_task: NotRequired[dict]  # ← 待办任务：一等公民
    log: NotRequired[Annotated[list[str], lambda a, b: (a or []) + b]]


# ─────────────────────────── 节点 ───────────────────────────

#: 主 agent 的假模型，模块级持有以便响应按序消费：
#:   第 1 次调用 → 正确派发 guided-learning
#:   第 2 次调用 → ⚠️ 故意"困惑"，派发**错误的** lesson-prep
#: 若续跑走了 chat 节点，就会消费到第 2 条，从而暴露"LLM 会选错"。
_MAIN_MODEL = ToolFakeChatModel(
    responses=[
        tc("dispatch", {"skill": "guided-learning", "params": "年级=三年级 学科=数学"}, "g1"),
        tc("dispatch", {"skill": "lesson-prep", "params": "年级=三年级（LLM 选错了）"}, "x1"),
    ]
)


def chat(state: State) -> dict:
    """主 agent 的 LLM 决策节点。只负责「启动」。"""
    msg = _MAIN_MODEL.invoke(state["messages"])
    picked = msg.tool_calls[0]["args"]["skill"] if getattr(msg, "tool_calls", None) else "?"
    return {"messages": [msg], "log": [f"chat: LLM 决策 → 它选了 {picked}"]}


def dispatch(state: State) -> dict:
    """派发执行器。缺参数时把 pending_task 写进 state —— **由代码写，不由 LLM 写**。"""
    call = state["messages"][-1].tool_calls[0]
    skill = call["args"]["skill"]
    result = EXECUTORS[skill](call["args"]["params"])
    out = [f"dispatch: 调用 {skill} → {result[:40]!r}"]

    update: dict = {
        "messages": [AIMessage(content=f"【{skill}】{result}")],
        "log": out,
    }
    if result.startswith("NEEDS_INPUT"):
        update["pending_task"] = {
            "skill": skill,                 # ← 代码写入，不是 LLM 决定
            "missing": result.split("|")[1],
            "params": call["args"]["params"],
        }
        out.append(f"dispatch: 写入 pending_task(skill={skill})")
    return update


def ask_user(state: State) -> dict:
    """向用户提问（此处用 interrupt 或正文皆可，不影响路由确定性）。"""
    task = state["pending_task"]
    return {
        "messages": [AIMessage(content=f"请补充：{task['missing']}")],
        "log": [f"ask_user: 就 {task['skill']} 的缺失项提问"],
    }


def resume_task(state: State) -> dict:
    """续跑：**纯代码，LLM 没有任何参与**。"""
    task = state["pending_task"]
    skill = task["skill"]
    answer = state["messages"][-1].content  # 用户答复

    # 按名字查表调用 —— 不是 LLM 选择
    result = EXECUTORS[skill](f"{task['params']} {answer}")
    return {
        "messages": [AIMessage(content=f"【{skill}】{result}")],
        "pending_task": None,
        "log": [f"resume_task: 查表命中 {skill}，未经过 LLM"],
    }


# ─────────────────────────── 路由（全部确定性） ───────────────────────────

def entry_route(state: State) -> str:
    """入口路由：有待办任务就直连续跑，**不经过 LLM**。"""
    return "resume_task" if state.get("pending_task") else "chat"


def after_chat(state: State) -> str:
    last = state["messages"][-1]
    return "dispatch" if getattr(last, "tool_calls", None) else END


def after_dispatch(state: State) -> str:
    return "ask_user" if state.get("pending_task") else END


def build():
    g = StateGraph(State)
    g.add_node("chat", chat)
    g.add_node("dispatch", dispatch)
    g.add_node("ask_user", ask_user)
    g.add_node("resume_task", resume_task)
    g.add_conditional_edges(START, entry_route, ["chat", "resume_task"])
    g.add_conditional_edges("chat", after_chat, ["dispatch", END])
    g.add_conditional_edges("dispatch", after_dispatch, ["ask_user", END])
    g.add_edge("ask_user", END)
    g.add_edge("resume_task", END)
    return g.compile(checkpointer=InMemorySaver())


def main() -> None:
    print("=" * 74)
    print("实验 07：答复能否保证传回正确的执行器？")
    print("=" * 74)

    graph = build()
    cfg = {"configurable": {"thread_id": "route-test"}}
    seen = 0

    def show(result: dict) -> None:
        """只打印本轮新增的日志行（state 跨轮累积，全量打印会看不出增量）。"""
        nonlocal seen
        for line in result["log"][seen:]:
            print(f"   {line}")
        seen = len(result["log"])

    # ── 第 1 轮：用户选【导学单】，主 agent 派发 ──
    print("\n【第 1 轮】用户：帮我出导学单")
    r1 = graph.invoke({"messages": [HumanMessage("帮我出导学单")], "log": []}, cfg)
    show(r1)
    print(f"   → pending_task = {r1.get('pending_task')}")
    print(f"   → 主 agent 提问 = {r1['messages'][-1].content!r}")
    print(f"   → 调用计数 = {CALLS}")

    # ── 第 2 轮：用户答复 ──
    print("\n【第 2 轮】用户答复：1 课时")
    r2 = graph.invoke({"messages": [HumanMessage("1 课时")], "log": []}, cfg)
    show(r2)
    print(f"   → pending_task = {r2.get('pending_task')}")
    print(f"   → 最终消息 = {r2['messages'][-1].content!r}")
    print(f"   → 调用计数 = {CALLS}")

    print("\n" + "-" * 74)
    ok = CALLS["guided-learning"] == 2 and CALLS["lesson-prep"] == 0
    print(f"   guided-learning 被调用 {CALLS['guided-learning']} 次（1 次派发 + 1 次续跑）")
    print(f"   lesson-prep     被调用 {CALLS['lesson-prep']} 次（困惑的 LLM 想派发它，但没轮到它）")
    print(f"\n   → {'✅ 答复精确回到 guided-learning，LLM 全程没有参与路由' if ok else '❌ 路由出错'}")
    print()


if __name__ == "__main__":
    main()
