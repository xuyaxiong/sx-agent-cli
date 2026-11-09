"""把「问」这条链**真渲染一遍** —— 从执行器申报到用户答复投递，一行一行看。

    uv run python -m experiments.ask_flow_log

🔴 **为什么要跑它**（用户 2026-11-05）：

> *"这个请求信息相关的 5 条，含义太模糊了，没头没脑了，难以阅读审计。"*
>
> *"朴素来说，问问题你知道吧 —— 关注**谁在向谁问一个什么问题**，你记录必须把这个
>   搞清楚；**谁答不上被转发给谁了**；**谁又答复了谁的什么问题**。"*

这一串原来散在 5 条 note 里，每条各说各的：`R01` 和 `轮次=2` 并排（同一个词指两件事）、
`🔙 RETURN` 显示的是**流程说明**而不是这一次、那句问题**贴了 4 遍**。

⚠️ **它只是"看"** —— 判据在 `evals/unit_ask_flow.py`（14 条，会红）。
**驱动是同一份**（`unit_ask_flow.drive`）：跑一次，这边打印、那边断言，
所以**看到的和守着的永远是同一件事**。

⚠️ 它**会往真日志文件里写**（同 `unit_decision` / `unit_ask_flow`）。
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import chat
from evals.unit_ask_flow import drive, sample_task
from proto import trace
from proto.handoff.briefing import asked_text

_SEEN: list[tuple[str, str]] = []


def main() -> None:
    from proto.graph import next_pending

    real_emit = chat._emit
    chat._emit = lambda *a, **k: _SEEN.append((a[0], a[1]))  # type: ignore[assignment]
    trace.subscribe(chat._on_event)

    print("═" * 78)
    print("「问」这条链 —— 每一跳都写清 `谁 → 谁：什么`")
    print("═" * 78)

    def _show(title: str, events: list[dict], lines: str) -> None:
        """**每一跳跑完就打印** —— 等 `drive()` 整个返回再打，展示层那几行会全挤在最后。"""
        print(f"\n{'─' * 78}\n【{title}】\n{'─' * 78}\n  ── 终端 ──")
        for role, text in _SEEN:
            head = chat._STYLE.get(role, (role.upper(), ""))[0]
            print(f"  {head:<11}┃ " + text.replace("\n", "\n" + " " * 15 + "┃ "))
        _SEEN.clear()
        print("  ── 日志文件（记录层 —— `📤 交接` 那几行只在这儿）──")
        for line in lines.splitlines():
            if line.strip():
                print("   " + line)
        # 透传那一跳之后，用户**看到问题**的地方是 CLI 的答复提示符
        # （`chat.py` 那两行，真调用点）。认的是**事件**（`hop` 字段），不是标题措辞。
        if any(e.get("type") == "handoff" and e.get("hop") == "待答透传" for e in events):
            p = next_pending({"tasks": [sample_task()]})
            print("\n  ── 用户看到的（CLI 答复提示符，`chat.next_pending` ＋ `asked_text`）──")
            print(f"  💬 ASK      ┃【{p.get('skill')}】问：{asked_text(p)}")

    drive(on_phase=_show)
    chat._emit = real_emit  # type: ignore[assignment]


if __name__ == "__main__":
    main()
