"""单元级：**裸原型 CLI 的显示层**（`proto/__main__.py`，不调 LLM）。

    uv run python -m evals.unit_proto_cli

**为什么要有它**（2026-11-06，外审 N2 的收尾）：

`python -m proto` 的显示层原来**从 `state` 里渲染**（`state["messages"]` / `state["log"]`），
于是平台透传的那条 `AIMessage`（装的是**执行器的原话**）被印成 `🤖 主 agent 输出` ——
来源标错，而 `proto/trace.py` 那条"展示层不读记录层产物"的规矩本来是防这个的
（`evals/chat.py` 早就改成订阅事件了）。现在这一层也订阅事件，**归属由事件自己带着**。

🔴 **这条单测守的是那个"不再有第二张表"的取舍**：这一层**故意不建** `kind → 角色`
映射表（建了就是第二份，加一个 kind 要改两处、而只有 `chat` 那张有守卫 `unit_console` ⑪）。
它按**形状**渲染，认不出的一律照打 —— 所以这里要断的是：**八种形状都有输出**，
**没表过态的 kind 也不会静默消失**（fail-open）。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import contextlib
import io
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os

# 🔴 **单测的日志打 tag**（2026-11-06）：`logs/` 里真会话与测试垃圾混在一起，复盘时
#    grep 不动 —— 打上 tag 后文件名是 `proto-<ts>-unit-<名字>.log`，一眼可辨
#    （清理：`rm logs/*-unit-*.log`）。复用跑批那个 `PROTO_LOG_TAG`（`evals/_child.py`）。
# ⚠️ **必须排在所有非标准库 import 之前**：`proto.trace` 在 import 那一刻读 env 定文件名，
#    而 `from evals import chat` 这种**间接** import 也会把它拉进来（实测踩过：
#    第一版插在 `from evals import chat` 之后 → 日志照样没 tag）。
#    守卫：`unit_feed_waiting` ⑮。
os.environ["PROTO_LOG_TAG"] = "unit-proto-cli"
os.environ["PROTO_LOG_DIR"] = "tmp/logs"   # 🔴 测试日志是**噪音** —— 别混进给人看的 `logs/`

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from proto import recorder, trace
from proto.handoff.decision import emit_decision
from proto.__main__ import _on_event

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail else ""))


#: ⚠️ **只订阅一次**（`trace.subscribe` 是 append，没有退订）—— 每次 `_capture` 都挂一遍
#: 的话，同一条事件会被印 N 遍（第一版就是这么写的，输出里同一行出现 4 次）。
trace.subscribe(_on_event)


def _capture(fn) -> str:
    """跑一段，把**订阅者印出来的**收回来（订阅已在模块级挂好）。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


def main() -> None:
    print("① 八种事件形状**各有输出**（展示层不许静默丢）")
    # ⚠️ 判据：形状是**受控**的（`trace._EVENT_TYPES` 打错当场炸）——
    #    所以这里可以穷举：漏一种，就是那一种在屏幕上永远看不见。
    out = _capture(lambda: (
        trace.section("试答 · 一次决策", kind="ask_main"),
        trace.note("推回去：既没交付、也没申报", kind="nudge"),
        trace.block("发给执行器的内容", "第一行\n第二行"),
        # ⚠️ 用**工具返回**当这条的形状样本：`dump` 里 AI 的正文**故意不重印**
        #    （它已由 `stream` 实时播过 —— 同 `chat._on_dump` 的契约，见 ③）
        trace.dump_trace("主 agent 第 1 步", [ToolMessage("技能正文……", tool_call_id="call_x")],
                         role=trace.AGENT),
        emit_decision("主 agent", q="够不够？", given=["产物 1 个"], result=["completed"],
                       nxt="交付", result_by="platform"),
        trace.cut("台账", kept=100, total=500, see="全文见日志"),
        trace.handoff("回执", "执行器", "主 agent", "回执正文"),
    ))
    # ⚠️ `handoff` 是**故意不上屏**的（同 `evals/chat.py` 的契约：它那一跳上面已经播过）——
    #    所以这里断的是"**除它以外**每一种形状都有输出"。
    for name, needle in (
        ("section", "试答 · 一次决策"),
        ("note", "推回去"),
        ("block", "第一行"),
        ("dump", "的返回"),
        ("decision", "够不够？"),
        ("cut", "✂️ 截断"),
    ):
        check(f"{name} 有输出", needle in out, repr(needle))
    check("`handoff` **故意不上屏**（只进日志 —— 同 chat 的契约）", "回执正文" not in out)

    print("\n② 🔴 平台透传印成「平台 → 用户」，**不是** 🤖（外审 N2 的病根）")
    # 这条是**契约**：透传那句是**执行器的原话**、平台只是转达 —— 归属由 `kind` 给。
    # 原来这一层从 `state["messages"]` 渲染，只能看见"一条 AIMessage" → 印成主 agent 说的。
    relay = _capture(lambda: trace.note("【待答】- 「lesson-prep」问：课时怎么安排？", kind="relay"))
    check("印成 `📮 平台 → 用户：…`", "📮 平台 → 用户：" in relay, repr(relay[:60]))
    check("**没有**印成 `🤖`（来源不许标错）", "🤖" not in relay, repr(relay[:60]))

    print("\n③ 消息归属按 `role` 取 —— 不读标记、不猜措辞")
    # ⚠️ 归属印在**工具调用那一行**（`🧠 {who} → 工具名`）—— AI 的正文由 `stream` 播，
    #    `dump` 只补"它调了什么、拿到了什么"（同 `chat._on_dump`）。
    def _one_dump(tag: str, role: str) -> None:
        trace.dump_trace(tag, [AIMessage("", tool_calls=[
            {"name": "read_skill", "args": {"skill_name": "quiz"}, "id": f"call_{role}"}])],
            role=role)

    both = _capture(lambda: (_one_dump("主 agent 第 1 步", trace.AGENT),
                             _one_dump("执行器 R01 第 1 轮", trace.EXECUTOR)))
    check("主 agent 的 dump 标 `🤖 主 agent`", "🤖 主 agent →" in both, repr(both[:100]))
    check("执行器的 dump 标 `🔧 执行器`", "🔧 执行器 →" in both, repr(both[:100]))
    # ⚠️ 故意把 `tag` 写成会误导的样子：归属**只看 role**（`who`/`tag` 是给人看的自由文本，
    #    从措辞里认角色正是 2026-11-03 修掉的那种失配）。
    tricky = _capture(lambda: _one_dump("主 agent 第 1 步", trace.EXECUTOR))
    check("`tag` 里写着「主 agent」也不许影响归属（只认 role）",
          "🔧 执行器 →" in tricky and "🤖" not in tricky, repr(tricky[:100]))

    print("\n④ 切了必须标（判据②）")
    long_block = "\n".join(f"第 {i} 行" for i in range(40))
    capped = _capture(lambda: trace.block("很长的块", long_block))
    check("超长块被截行", "第 39 行" not in capped)
    check("**但必须说清**还剩多少行 ＋ 全文在哪", "还有" in capped and "全文见日志" in capped,
          repr(capped[-120:]))

    print("\n⑤ fail-open：**没表过态的 kind 也不许静默消失**")
    # ⚠️ 这一层**故意没有** `kind → 角色` 表（见模块 docstring）—— 新加一个 kind 时，
    #    它不会被"忘了登记"吃掉：形状认得，就照打。这条守的正是那个取舍。
    fresh = _capture(lambda: (
        trace.note("一个展示层没专门处理过的 kind", kind="delivered"),
        trace.section("另一个", kind="run_end"),
    ))
    check("note 照打", "没专门处理过的 kind" in fresh, repr(fresh[:80]))
    check("section 照打", "另一个" in fresh, repr(fresh[:80]))

    print("\n⑥ 工具返回带**工具名**（靠 `(role, tool_call_id)` 配对，不按顺序猜）")
    pairs = _capture(lambda: trace.dump_trace(
        "主 agent 第 1 步",
        [AIMessage("", tool_calls=[{"name": "read_skill", "args": {"skill_name": "quiz"},
                                    "id": "call_abc"}]),
         ToolMessage("技能正文……", tool_call_id="call_abc")],
        role=trace.AGENT,
    ))
    check("工具调用印了名字与参数", "read_skill" in pairs and "quiz" in pairs, repr(pairs[:100]))
    check("它的返回也认得出是谁的（按 id 配对）", "read_skill 的返回" in pairs, repr(pairs[:120]))

    # 形状穷举守卫：`_EVENT_TYPES` 里每一种，这一层都得**表态**（上屏 / 有专门分支 / 只进日志）
    print("\n⑦ 形状穷举：`trace._EVENT_TYPES` 每一种都在这一层表过态")
    import inspect

    src = inspect.getsource(_on_event)
    _handled = {"stream", "section", "note", "block", "decision", "cut", "dump"}
    _log_only = {"handoff"}
    _missing = set(trace._EVENT_TYPES) - _handled - _log_only   # noqa: SLF001
    check("八种形状：7 种上屏 ＋ 1 种（handoff）只进日志，**没有第四种下场**",
          not _missing, f"没表态的：{sorted(_missing)}")
    check("`handoff` 的下场写在注释里（不是默默不管）", "只进日志" in src)

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
