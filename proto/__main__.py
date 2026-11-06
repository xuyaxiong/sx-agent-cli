"""交互式跑原型（**裸原型**入口）。

    uv run python -m proto                          # 交互式
    uv run python -m proto --say "帮我备课，三年级数学"   # 单轮

⚠️ **产品入口是 `evals/chat.py`**（打包打的就是它，见 `build_exe.py`）—— 这里是
**内置玩具技能、显示层最简**的那个入口，用来快速看机制。

🔴 **显示层订阅事件总线**（2026-11-06，外审 N2 的收尾）：契约是**事件**，不是文本 ——
和 `evals/chat.py` 同一条规矩（`proto/trace.py`：展示层不读记录层的产物）。
**原来这里从 `state["messages"]` / `state["log"]` 里渲染**，于是平台透传的那条
`AIMessage`（装的是**执行器的原话**）被印成 `🤖 主 agent 输出` —— 来源标错。
现在"谁说的"由**事件自己带着**（`role` / `kind`），归属不可能再标错。

⚠️ **不建第二张 kind→角色 表**（那正是"两套"）：`chat._ROLE_BY_KIND` 是它的表态，
这里是**另一份**，加一个 kind 就要改两处、而只有它那张有守卫（`unit_console` ⑪）——
漏一处就是**静默丢一条**。所以这一层**按形状渲染**：认得的形状各给一行，
**认不出的一律照原样打出来**（fail-open —— 排版丑一点没关系，静默丢才要命）。
"""

from __future__ import annotations

import argparse
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from langchain_core.messages import HumanMessage

from proto import trace
from proto.config import describe
from proto.graph import build, next_pending
from proto.handoff.briefing import asked_text
from proto import artifacts as artifacts_mod

BAR = "─" * 74

#: 块最多印几行（**切了必须标** —— 判据②；全文永远在日志文件里）
_BLOCK_MAX_LINES = 12


# ──────────────────────────── 显示层：事件 → 屏幕 ────────────────────────────

#: 说话的是谁 —— 按 `role` **枚举**取（逻辑），不从 `who` 的措辞里猜。
_ROLE_TAG = {trace.AGENT: "🤖 主 agent", trace.EXECUTOR: "🔧 执行器"}

_last_stream_tag = ""      # 同一角色连着吐字时，前缀只打一次
_tool_names: dict[tuple[str, str], str] = {}   # (role, tool_call_id) → 工具名


def _bullets(v: object) -> str:
    """列表 → 逐项 `- x`；字符串原样。**空的显式写「（无）」**（"没有"也要能审计）。

    ⚠️ 这里**不截断** —— 四格是一次判断的**凭据**，看全才有意义（同 `handoff.decision`
    的"判断必须可审计"）。长的是 `given`/`result` 里的自由文本，那是它本来该有的样子。
    """
    if isinstance(v, (list, tuple)):
        return "\n".join(f"- {x}" for x in v) if v else "（无）"
    return str(v) or "（无）"


def _capped(text: str) -> str:
    """多行文本**截行**并**显式说明**（判据②：切了必须标，绝不静默截）。"""
    lines = str(text).splitlines() or [""]
    if len(lines) <= _BLOCK_MAX_LINES:
        return "\n".join(lines)
    keep = lines[:_BLOCK_MAX_LINES]
    return "\n".join([*keep, f"…（还有 {len(lines) - _BLOCK_MAX_LINES} 行，全文见日志文件）"])


def _on_event(ev: dict) -> None:
    """事件总线的订阅入口 —— 展示层从这里拿**结构化**事实，自己排版上屏。"""
    global _last_stream_tag
    t = ev.get("type")
    if t != "stream":
        _last_stream_tag = ""   # 换个形状就把流式前缀重置（下次吐字重新标谁在说）

    if t == "stream":
        tag = _ROLE_TAG.get(ev.get("role") or "", f"· {ev.get('who') or '平台'}")
        if tag != _last_stream_tag:
            print(f"\n{tag}：")
            _last_stream_tag = tag
        print(f"  {ev['text']}")
    elif t == "section":
        print(f"\n{'═' * 3} {ev['title']}")
    elif t == "note":
        if ev.get("kind") == "relay":
            # 🔴 **平台透传**：这句是**执行器的原话**，平台只是转达 —— 归属由 kind 给，
            #    不读文本、不读标记（判据一）。原文照上屏（**不切前缀**：
            #    日志文件那个 `finalize: ` 是记录层的格式，切它就会随文案漂 —— 实测踩过）。
            print(f"\n📮 平台 → 用户：{ev['text']}")
        else:
            print(f"   │ {ev['text']}")
    elif t == "block":
        print(f"   │ {ev['label']}")
        for ln in _capped(ev["text"]).splitlines():
            print(f"   │   {ln}")
    elif t == "decision":
        # 四格从**对象**渲染（前提 / 问题 / 结果 / 下一步）—— 不从渲染文本反推
        print(f"   │ 🧭 {ev['who']}｜问题：{ev['q']}")
        print(f"   │   前提：{_bullets(ev['given'])}")
        print(f"   │   结果：{_bullets(ev['result'])}")
        print(f"   │   下一步：{ev['next']}")
    elif t == "cut":
        print(f"   │ ✂️ 截断 · {ev['where']}：保留 {ev['kept']} 字 / 原文 {ev['total']} 字"
              + (f"｜{ev['see']}" if ev.get("see") else ""))
    elif t == "dump":
        _on_dump(ev)
    # `handoff`（交接记录）只进日志文件，不上屏 —— 同 `evals/chat.py` 的契约：
    # 它的内容上面那些形状已经各自播过一遍，这里再播就是同一个事实两副面孔。


def _on_dump(ev: dict) -> None:
    """消息轨迹 —— **消息对象本身就在事件里**，归属按 `ev["role"]` 取。"""
    role = ev.get("role") or ""
    who = _ROLE_TAG.get(role, f"· {role or '未知角色'}")
    for m in ev.get("messages") or []:
        for c in getattr(m, "tool_calls", None) or []:
            if c.get("id"):
                _tool_names[(role, str(c["id"]))] = str(c.get("name"))
            print(f"   │ 🧠 {who} → {c.get('name')}（{trace.brief_args(c.get('args') or {})}）")
        kind = type(m).__name__
        if kind == "AIMessage":
            continue        # 它说的话已由流式实时播过（同 `chat._on_dump` 的契约）
        content = str(m.content)
        if kind == "ToolMessage":
            name = _tool_names.get((role, str(getattr(m, "tool_call_id", "") or "")), "?")
            print(f"   │ ↩ {name} 的返回（{len(content)} 字）：\n{_capped(content)}")
        elif kind == "SystemMessage":
            # 平台每轮注入的台账 —— **不截断**（它是"平台塞了什么"的观察窗口）
            print(f"   │ 📌 平台 → 主 agent（注入的台账）：\n{content}")


# ──────────────────────────── 快照读数 ────────────────────────────


def _ctx_chars(messages: list) -> int:
    total = 0
    for m in messages:
        total += len(str(m.content))
        for c in getattr(m, "tool_calls", None) or []:
            total += len(str(c.get("args", "")))
    return total


def _dump(result: dict) -> None:
    """回合末的**读数**（Task 表 ＋ 上下文体积）—— 事件里没有的东西，从快照读。"""
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
    trace.subscribe(_on_event)      # 🔴 显示层从这一刻起只认事件

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
            _dump(result)
            print(BAR)

    trace.close()
    print(f"完整日志：{trace.LOG_FILE}")


if __name__ == "__main__":
    main()
