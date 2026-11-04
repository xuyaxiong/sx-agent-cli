"""交互式跑原型。

    uv run python -m proto                          # 交互式
    uv run python -m proto --say "帮我备课，三年级数学"   # 单轮

新形状：**主 agent 是完整的 agent**，自己决定「直接答 / write_artifact 自己产出 / delegate 委派」。
平台在旁边注入任务台账、兜底补问。**不委派就没有 Task。**
"""

from __future__ import annotations

import argparse
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from langchain_core.messages import AIMessage, HumanMessage

from proto import trace
from proto.config import describe
from proto.graph import build
from proto import artifacts as artifacts_mod

BAR = "─" * 74


def _print_delta(result: dict, seen: list[int]) -> None:
    log = result.get("log") or []
    for line in log[seen[0] :]:
        print(f"   │ {line}")
    seen[0] = len(log)


def _print_new_messages(result: dict, seen: list[int]) -> None:
    msgs = result.get("messages") or []
    for m in msgs[seen[0] :]:
        kind = type(m).__name__
        if kind == "AIMessage":
            calls = [c.get("name") for c in (getattr(m, "tool_calls", None) or [])]
            if calls:
                print(f"   │ 🧠 主 agent 决定调工具：{calls}")
            if str(m.content).strip():
                print(f"\n🤖 {m.content}\n")
        elif kind == "SystemMessage":
            print(f"   │ 📌 {m.content}")
    seen[0] = len(msgs)


def _ctx_chars(messages: list) -> int:
    total = 0
    for m in messages:
        total += len(str(m.content))
        for c in getattr(m, "tool_calls", None) or []:
            total += len(str(c.get("args", "")))
    return total


def _dump(result: dict) -> None:
    tasks = result.get("tasks") or []
    print(f"   │ ── Task（只记委派出去的工作，共 {len(tasks)} 个）──")
    for t in tasks:
        print(
            f"   │ {t['id']} skill={t['skill']:<16} stage={t['stage']:<14} "
            f"产物={t['artifacts'] or '（无）'}"
        )
        if t.get("agent_tried"):
            print(f"   │     主 agent 试答过；执行器说={t['report']!r}")
    ctx = _ctx_chars(result.get("messages") or [])
    body = artifacts_mod.chars()
    print(f"   │ ★ 主 agent 上下文 = {ctx} 字 ｜ 产物库正文 = {body} 字")
    if body and ctx:
        print(f"   │ ★ 隔离效果：产物正文是主上下文的 {body / ctx:.1f} 倍，全在产物库里")


def main() -> None:
    ap = argparse.ArgumentParser(description="主 agent（完整 agent）+ Skill / Task / Sub Agent")
    ap.add_argument("--thread", default="proto-1")
    ap.add_argument("--say", help="非交互：只跑这一句")
    ap.add_argument("--answer", default="难度要基础的", help="--say 模式下自动应答")
    args = ap.parse_args()

    graph = build()
    config = {"configurable": {"thread_id": args.thread}}
    seen_log = [0]
    seen_msg = [0]

    print(BAR)
    print(f"主 agent = 完整 agent（自己决定：直接答 / 自己干 / 委派） · 模型={describe()}")
    print(f"日志：{trace.LOG_FILE}")
    print(BAR)

    def turn(text: str) -> dict | None:
        # 🔴 **炸一轮 ≠ 结束会话**（2026-11-04 复审 N2/R8）：一次基础设施抖动
        # （`stream_agent` 重试过仍失败）原来会把整个进程带走。现在按**轮**兜住，
        # 说清原因、返回 None，让调用方接着问下一句。
        try:
            result = graph.invoke({"messages": [HumanMessage(text)], "log": []}, config)
        except Exception as e:  # noqa: BLE001
            print(f"\n⚠️ **这一轮没跑成**（平台故障）：{type(e).__name__}: {e}")
            print("   会话还在 —— 把刚才那句**再说一遍**就行。\n")
            return None
        if args.say:
            # 有待答就自动补一轮（模拟用户答复）
            for _ in range(3):
                waiting = [t for t in (result.get("tasks") or []) if t.get("stage") == "waiting_for_input"]
                if not waiting:
                    break
                print(f"\n❓ 执行器说：{waiting[0]['report']}")
                print(f"   自动答复：{args.answer}")
                result = graph.invoke(
                    {"messages": [HumanMessage(args.answer)], "log": []}, config
                )
        return result

    if args.say:
        print(f"\n👤 {args.say}")
        result = turn(args.say)
        if result is None:
            trace.close()
            return
        _print_delta(result, seen_log)
        _print_new_messages(result, seen_msg)
        _dump(result)
        print(BAR)
        trace.close()
        return

    print("输入你的请求（空行退出）。试试这些：\n")
    print("  · 你好                              → 主 agent 应该**自己答**（不建 Task）")
    print("  · 帮我备课，三年级数学               → 需要专业能力 → 委派给隔离执行器")
    print("  · 帮我备课、导学单，再出个随堂小测，三年级数学\n")
    while True:
        try:
            text = input("👤 ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            break
        result = turn(text)
        if result is None:
            continue          # 这一轮没跑成 → 接着问下一句（会话不死）
        _print_delta(result, seen_log)
        _print_new_messages(result, seen_msg)
        _dump(result)
        print(BAR)

    trace.close()
    print(f"完整日志：{trace.LOG_FILE}")


if __name__ == "__main__":
    main()
