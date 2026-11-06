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
from proto.graph import build, next_pending
from proto.handoff.briefing import asked_text
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
    ap = argparse.ArgumentParser(description="主 agent（完整 agent）＋ 技能 / Task / 执行器")
    ap.add_argument("--thread", default="proto-1")
    ap.add_argument("--say", help="非交互：只跑这一句")
    ap.add_argument("--answer", default="",
                    help="--say 模式下自动应答（不给就把问题打出来停住，不替用户编答案）")
    args = ap.parse_args()

    graph = build()
    config = {"configurable": {"thread_id": args.thread}}
    seen_log = [0]
    seen_msg = [0]

    print(BAR)
    print(f"主 agent = 完整 agent（自己决定：直接答 / 自己干 / 委派） · 模型={describe()}")
    print(f"日志：{trace.LOG_FILE}")
    print(BAR)

    def turn(text: str, answer_to: str = "") -> dict | None:
        # 🔴 **炸一轮 ≠ 结束会话**（2026-11-04 复审 N2/R8）：一次基础设施抖动
        # （`stream_agent` 重试过仍失败）原来会把整个进程带走。现在按**轮**兜住，
        # 说清原因、返回 None，让调用方接着问下一句。
        #
        # `answer_to`：**这条消息是在回答哪个待答任务**（`solution.md` §0.65 Y1）——
        # 由**通道**填（下面那个 `↩` 提示符），平台不猜。
        init: dict = {"messages": [HumanMessage(text)], "log": []}
        if answer_to:
            init["answer_to"] = answer_to
        try:
            result = graph.invoke(init, config)
        except Exception as e:  # noqa: BLE001
            print(f"\n⚠️ **这一轮没跑成**（平台故障）：{type(e).__name__}: {e}")
            print("   会话还在 —— 把刚才那句**再说一遍**就行。\n")
            return None
        if args.say:
            # 有待答就自动补一轮（模拟用户在 `↩` 提示符下答复）—— **带标识**投回去
            for _ in range(3):
                p = next_pending(result)
                if p is None:
                    break
                print(f"\n❓【{p.get('skill')}】问：{asked_text(p)}")
                # 🔴 **没给 `--answer` 就不自动应答**（2026-11-06）：默认值原来是某次
                #    测试留下的 `"难度要基础的"` —— 作为通用默认值，它是在**替用户编一个
                #    没说过的前提**（正是这套代码反复防的事）。把问题打出来就停。
                if not args.answer:
                    print("   （没给 `--answer` —— 不自动应答；要自动答就带上 `--answer '…'`）")
                    break
                print(f"   自动答复：{args.answer}")
                result = turn(args.answer, answer_to=p["id"])
                if result is None:
                    break
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
    print("  · 帮我备课，三年级数学               → 需要专业技能 → 委派给隔离执行器")
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

        # 🔴 **待答：一次摆一个**（`solution.md` §0.65 Y1）——
        #    这个提示符**就是"通道"**：在它下面打的字**属于那一条请求**（带 `answer_to` 投回去）。
        #    **直接回车 = 跳过** → 回到 `👤 `，在那里打的字只给主 agent（这条待答继续挂着）。
        while (p := next_pending(result)) is not None:
            q = asked_text(p)
            print(f"\n❓【{p.get('skill')}】问：{q}")
            try:
                ans = input("↩ 答它（回车跳过，回 👤 说别的）> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not ans:
                break
            result = turn(ans, answer_to=p["id"])
            if result is None:
                break
            _print_delta(result, seen_log)
            _print_new_messages(result, seen_msg)
            _dump(result)
            print(BAR)

    trace.close()
    print(f"完整日志：{trace.LOG_FILE}")


if __name__ == "__main__":
    main()
