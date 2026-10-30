"""交互式跑一个 case —— **用户自己当用户**（人在环）。

    uv run python -m evals.chat --case t2-same-turn

与 `evals.run` 的区别：`turns` 不再是预置字符串，**问题由你当场回答**。
预置答复是照着技能里"会问什么"编的，几乎必然答得上 —— 那恰好绕开了
"答非所问 / 一次答不全 / 用户反问"这类真实情况。这个入口就是用来压它们的。

跑完落一份与跑批**同格式**的记录：`runs/<tag>/artifacts/<case>__r1/_run.md`
（逐轮全记录 + 产物原文 + 原始 trace），另有 `results.json` 供回看。
"""

# ruff: noqa: T201  # 交互式入口，输出即界面

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
import threading
from pathlib import Path

import yaml
from langchain_core.messages import AIMessage, HumanMessage

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from evals._child import (  # noqa: E402
    _RECEIPT_KINDS,
    _split_receipt,
    dump_artifacts,
    dump_run_record,
    install,
    snapshot,
)
from proto import trace  # noqa: E402
from proto.graph import build  # noqa: E402
from proto.skills import ARTIFACTS  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BAR = "─" * 74


def _load_case(which: str) -> tuple[Path, dict]:
    p = Path(which)
    if not p.exists():
        p = HERE / "cases" / f"{which}.yaml"
    if not p.exists():
        raise SystemExit(f"找不到 case：{which}")
    return p, yaml.safe_load(p.read_text(encoding="utf-8"))


# ──────────────────────────── 输出层：一套视觉语言 ────────────────────────────
#
# 用户要求（2026-10-29）：*"输出太乱了 我要显式的看到时间信息 用户输入是啥 ai输出是啥
# ai推理是啥 ai调用工具细节 等 你得通过不同的视觉元素来做到"*。
#
# ⚠️ **"AI 推理"没有单独通道** —— 实测抓不到 `reasoning_content`（模型经 OpenAI 兼容端点
#    不吐这个字段）。能给你的是最接近的两样：**它自己说的话**（决策叙述）+ **它实际调了什么
#    工具、参数是什么**（硬证据，不是它自称）。

#: 角色 →（标签 + ANSI 颜色）。颜色在非 tty / `NO_COLOR` 下自动关掉。
#:
#: 🔴 **标签一律纯 ASCII、不带图标**（用户 2026-10-30 定）。
#: 左对齐老是歪，两个来源都在"字符宽度"上：
#:   ① 中英混排 —— CJK 占 2 列、ASCII 占 1 列，各终端实现还有差异；
#:   ② **带变体选择符的 emoji**（`⚖️` = U+2696 + U+FE0F）—— 变体选择符是**零宽**的，
#:      而"码点 > 0x2E80 就算 2 列"的老算法会把它也算成 2，每个这种图标**多算 2 列**。
#: 全 ASCII 之后宽度就是"一个字符一列"，任何终端都对齐。
_STYLE: dict[str, tuple[str, str]] = {
    "user":   ("👤 USER",     "\033[96m"),   # 亮青
    "agent":  ("🧠 AGENT",    "\033[92m"),   # 亮绿
    "say":    ("🤖 EXECUTOR", "\033[96m"),   # 亮青
    "tool":   ("🔧 TOOL",     "\033[93m"),   # 亮黄
    "ret":    ("🔙 RETURN",   "\033[90m"),   # 灰
    "inject": ("📥 INJECT",   "\033[35m"),   # 紫
    "decide": ("🧭 DECIDE",   "\033[95m"),   # 亮紫
    "judge":  ("🧭 DECIDE",   "\033[95m"),   # 旧角色名，同 DECIDE
    "exec":   ("🚀 RUN",      "\033[94m"),   # 亮蓝
    "ask":    ("💬 ASK",      "\033[91m"),   # 亮红
    "plat":   ("📮 PLATFORM", "\033[90m"),   # 灰
    "done":   ("🏁 DONE",     "\033[92m"),   # 亮绿
    "task":   ("📋 TASKS",    "\033[90m"),
}

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

#: 实时播报：trace 日志里**值得推到终端**的行。左边是判据（子串），右边是角色。
#: 这些行**自带真实时间戳**（`proto.trace` 写的时候就打了），所以直接用它，不要自己补时间。
_LIVE: tuple[tuple[str, str], ...] = (
    # （主 agent 的工具调用由 dump 里的 `tool_call=` 播 —— 那行更全、带参数）
    ("delegate · ", "plat"),
    ("取代了 ", "plat"),
    ("run_task · R", "exec"),
    ("run_task 结束", "exec"),
    ("prepare · 投递用户原话", "plat"),
    ("prepare: 待答", "plat"),
    ("prepare: ✅", "done"),
    ("prepare: ⏳", "ask"),
    ("prepare: ❌", "ask"),
)


def _dw(s: str) -> int:
    """显示宽度（按**终端实际渲染**算，不按码点阈值）。

    三条规则，缺一条就会歪（每一条都实测踩过）：
      1. **零宽**：变体选择符（U+FE0E/FE0F）与 ZWJ 不占列 —— 老算法把 `⚖️` 的
         U+FE0F 也算成 2 列，于是每个这种图标**多算 2 列**
      2. **emoji 区**（U+1F000–1FAFF）+ **杂项符号区**（U+2600–27BF）一律 2 列 ——
         `❓`(U+2753) `✅`(U+2705) 的 East Asian Width 是 W，但 `🗣`(U+1F5E3) 是 N
         却仍是 emoji 默认呈现，靠 EAW 判会漏
      3. **带 VS16 的**按 emoji 呈现算 2 列 —— `↩️`(U+21A9+FE0F) 的 EAW 是 N，
         不加这条就会算成 1
      4. 其余按 East Asian Width：`W`/`F` 算 2，其他算 1
    """
    w, i, n = 0, 0, len(s)
    while i < n:
        c = s[i]
        if c in "︎️‍":  # 零宽：变体选择符 / ZWJ
            i += 1
            continue
        o = ord(c)
        if (
            (i + 1 < n and s[i + 1] == "️")  # 带 VS16 → emoji 呈现
            or 0x1F000 <= o <= 0x1FAFF
            or 0x2600 <= o <= 0x27BF
            or unicodedata.east_asian_width(c) in ("W", "F")
        ):
            w += 2
        else:
            w += 1
        i += 1
    return w


#: **正文层**（顶格、亮色）—— 只有"人和主 agent 的对话"。
#: 其余全部是**机制层**（缩进 + 暗灰 + `|`）：执行器的话、工具调用、判定、注入……
#: 用户 2026-10-30 定：*"正文输出 用户输入 和其它日志搅在一起了"* ——
#: 读对话只追顶格行，查机制看缩进行。
_LABEL_W = 12   # gutter 里标签的宽度（按显示列算；最长的 EXECUTOR/PLATFORM 是 11 列）
_TS = "[HH:MM:SS] "

#: 播报里已经打过的"执行器的话"（归一化前 60 字）—— 去重用。
#: 不去重的话，同一段话会以 🗣 EXECUTOR（实时）和 🧠 AGENT（小结）出现两次。
_SHOWN_EXEC: set[str] = set()


def _norm(s: str) -> str:
    return " ".join(s.split())[:60]


_MAIN_ROLES = frozenset({"user", "agent"})
_MECH_PREFIX = "   🔩 "     # 3 空格 + 🔩(2 列) + 空格 = 6 列
_MECH_COLOR = "\033[90m"     # 暗灰：机制日志统一压暗，靠标签区分类型


#: CLI 输出流落盘的 sink（`main()` 里打开）。**终端里滚过去的，文件里也得有** ——
#: 用户 2026-10-30：*"更详细的日志 尤其是我cli里面的运行流程 都要记录到文件里面"*。
#: 落的是**去色**的纯文本，和终端看到的内容逐行对应。
_SINK: "object | None" = None


def _to_file(line: str) -> None:
    if _SINK is not None:
        _SINK.write(line + "\n")
        _SINK.flush()


def paint(s: str, color: str = "") -> str:
    """上色（非 tty / `NO_COLOR` 时原样返回）。"""
    return f"{color}{s}\033[0m" if (_COLOR and color) else s


def _emit(role: str, text: str, stamp: str | None = None) -> None:
    """按统一版式打一条：`[时间] 标签 ┃ 内容`，续行对齐到 `┃`。

    正文层顶格；机制层多一个 `   | ` 前导并压成暗灰 —— 两层一眼分得开。
    """
    icon, color = _STYLE[role]
    stamp = stamp or time.strftime("%H:%M:%S")
    pad = " " * max(0, _LABEL_W - _dw(icon))
    if role in _MAIN_ROLES:
        head = f"[{stamp}] {icon}{pad}┃ "
        indent, tint = _dw(_TS) + _LABEL_W, color
    else:
        head = f"{_MECH_PREFIX}[{stamp}] {icon}{pad}┃ "
        indent, tint = _dw(_MECH_PREFIX) + _dw(_TS) + _LABEL_W, _MECH_COLOR
    cont = " " * indent + "┃ "
    lines = (text or "").splitlines() or [""]
    print(paint(head + lines[0], tint))
    _to_file(head + lines[0])
    for ln in lines[1:]:
        print(paint(cont + ln, tint))
        _to_file(cont + ln)


def _live(role: str, line: str) -> None:
    """把一条 trace 日志行推出去 —— 时间戳**用日志里的**（那才是事件真实发生的时刻）。"""
    m = re.match(r"\[(\d{2}:\d{2}:\d{2})\]\s*(.*)", line, re.S)
    if not m:
        return
    # 执行器的话给宽一点（提问往往在正文末尾，截太狠就看不到了）
    cap = 1000 if role == "say" else 400
    _emit(role, m.group(2)[:cap], stamp=m.group(1))


def _feed(st: dict, line: str) -> None:
    """把一行 trace 日志喂给状态机，该播的播出去。

    用户要的细节（2026-10-29）：*"工具调用 谁调用的 调用了什么工具 参数是什么 返回结果是什么…
    柔性的流程关键分支决策，一般都是调用 llm 实现，谁决策的，决策结果是什么，走了什么分支…
    平台注入的系统提示词 注入了什么内容 什么时候注入的 什么阶段（步骤）注入的"* ——
    所以这里不是"播报进度"，是把**因果链**摊开：

        谁（主 agent / 执行器 / 平台 / 判定）× 做了什么 × 参数或内容 × 结果
    """
    m = re.match(r"\[(\d{2}:\d{2}:\d{2})\]\s?(.*)", line, re.S)
    if m:
        st["ts"] = m.group(1)
        body = m.group(2)
        if st["block"] is not None:
            # ⚠️ `PROMPT（…）` / `RESPONSE（…）` 这两行**自带新时间戳**，但它们属于**同一个事件**
            # （`safe_structured` 先 section 再 block，两行都带 [ts]）——
            # 一律当"新事件"处理的话，块刚开就被冲掉，内容全丢（实测踩过）。
            if body.startswith(("PROMPT（", "RESPONSE（")):
                st["block"][1].append(body)
                return
            text = "\n".join(st["block"][1])  # 上一个多行块到此为止
            if len(text) > 1600:
                text = text[:1600] + f"\n…（本块共 {len(text)} 字，后面截了）"
            _emit(st["block"][0], text, stamp=st["block"][2])
            st["block"] = None

        if "消息轨迹" in body:  # 段落头：接下来的缩进行是谁的 dump
            st["section"] = "exec" if "run_task" in body else "agent"
            return
        if body.startswith("发给执行器的内容"):
            # 平台注入给执行器的**整份交接书**（任务 / 上次做到哪 / 历次答复 / 可读产物）
            st["block"] = ("inject", ["平台 → 执行器：本次交接的全部已知"], st["ts"])
            return
        if body.startswith("判定输入："):
            _emit("decide", "判定（LLM 决策）看的是什么：" + body[len("判定输入：") :], stamp=st["ts"])
            return
        if "轮判定：status=" in body:
            # 判定是 LLM 决策：**理由 / 进度 / 还缺** 都在它下面的续行里，一起收进块
            st["block"] = ("decide", ["【判定 · LLM 决策】" + body], st["ts"])
            return
        if "平台强制让主 agent 试答" in body:
            # 🔴 提问链路的关键一环：执行器问了 → **平台强制**让主 agent 先用自己的上下文试答。
            # 下面的块会把"参与决策的全部信息"（能力/任务/执行器的话/已做到/还缺/上下文）
            # 和决策结果（can_answer）一起收进来 —— 用户点名要看这个。
            st["section"] = "ask_main"
            st["block"] = (
                "decide",
                ["【主 agent 试答 · LLM 决策】平台强制的一步（在 `delegate` 内部）",
                 "参与决策的信息 ↓"],
                st["ts"],
            )
            return
        if "登记待答" in body:
            _emit("decide", "分支：主 agent 答不上 → " + body, stamp=st["ts"])
            return
        if body.startswith("finalize: 透传"):
            _emit(
                "ask",
                "平台 → 用户：" + body[len("finalize: ") :]
                + "\n（把上面那条**执行器的原话原样**转达 —— 代码保证不丢；主 agent 若也复述了一遍，那是提示词层面的礼貌，不是平台重复）",
                stamp=st["ts"],
            )
            return
        # （试答的 RESPONSE 由上面那个块一起收 —— 不单独再打一条，重复）
        for needle, role in _LIVE:
            if needle in body:
                _emit(role, body[:400], stamp=st["ts"])
                return
        return

    # ── 缩进行：要么在多行块里，要么在某个 dump 里 ──
    s = line.strip()
    if st["block"] is not None:
        st["block"][1].append(line[2:] if line.startswith("  ") else line)
        return
    if st["section"] not in ("exec", "agent"):
        return
    if re.match(r"\[\d+\] \w+\(", s):  # `[1] AIMessage(556字)`
        st["kind"] = s.split("] ", 1)[1].split("(", 1)[0]
        return
    if s.startswith("tool_call="):  # 谁调用了什么工具 + 参数
        rest = s[len("tool_call=") :]
        name = rest.split(" ", 1)[0]
        args = rest.split("args=", 1)[1] if "args=" in rest else ""
        st["calls"].append(name)
        who = "执行器" if st["section"] == "exec" else "主 agent"
        _emit("tool", f"{who} → {name}（{args[:300]}）", stamp=st["ts"])
        return
    if s.startswith("content="):
        body = s[len("content=") :].strip().strip("'").replace(chr(92) + "n", "\n")
        if st["kind"] == "ToolMessage":  # 工具**返回了什么**
            who = st["calls"].pop(0) if st["calls"] else "?"
            _emit("ret", f"{who} 的返回：\n{body[:600]}", stamp=st["ts"])
        elif st["kind"] == "SystemMessage":  # 平台注入的台账
            _emit("inject", f"平台 → 主 agent（每轮注入的任务台账）：\n{body[:600]}", stamp=st["ts"])
        elif st["kind"] == "AIMessage":
            # 执行器的话 → 机制层（🗣）；主 agent 的话 → **正文层**（🧠）。
            # ⚠️ 都从 dump 实时播：等轮末再打的话，主 agent 的话会跑到机制日志**前面**，
            # 时序就反了（用户 2026-10-30 指出："包括时序这些"）。
            if st["section"] == "exec":
                _SHOWN_EXEC.add(_norm(body))
                _emit("say", body, stamp=st["ts"])
            else:
                _emit("agent", body, stamp=st["ts"])


def _drain_once(pos: list[int]) -> None:
    """把日志里**还没读的**行读完（收尾用）。

    为什么需要：主 agent 的消息轨迹是这一轮最后写进日志的；tailer 在 `graph.invoke`
    返回后立刻停，最后那批行就漏了 —— 表现为"主 agent 的话只在回合小结里出现、顺序还错"。
    """
    path = Path(trace.LOG_FILE)
    st: dict = {"section": "", "kind": "", "ts": "", "calls": [], "block": None}
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            fh.seek(pos[0])
            for raw in fh:
                _feed(st, raw.rstrip("\n"))
            pos[0] = fh.tell()
    except OSError:
        pass
    if st["block"] is not None:  # 收尾时把没闭合的块也吐出来
        _emit(st["block"][0], "\n".join(st["block"][1]), stamp=st["block"][2])


def _tail_trace(stop: threading.Event, pos: list[int]) -> None:
    """**边跑边播**：跟着 trace 日志读，把值得看的行实时推出来。

    为什么绕这一圈：`graph.invoke` 是阻塞的，一轮 2–3 分钟、中途一个字都没有 ——
    用户看到的就是"敲完回车，终端死了"（2026-10-29 实测反馈）。
    而 trace 日志是**实时**写的，且每行自带真实时间戳 —— 所以时间是事件真实发生的时刻。
    """
    path = Path(trace.LOG_FILE)
    st: dict = {"section": "", "kind": "", "ts": "", "calls": [], "block": None}
    while not stop.wait(0.8):
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                fh.seek(pos[0])
                for raw in fh:
                    _feed(st, raw.rstrip("\n"))
                pos[0] = fh.tell()
        except OSError:
            pass


def _turn_summary(result: dict, elapsed: float) -> None:
    """回合小结：**只剩台账**。

    人和主 agent 的话、工具调用、判定、投递……全都已在**实时播报**（`_tail_trace`）里
    按真实时序打过了 —— 这里再打一遍就会重复，而且顺序是错的
    （小结在整轮跑完之后才执行）。用户 2026-10-30 指出："包括时序这些"。
    """
    tasks = result.get("tasks") or []
    if not tasks:
        return
    rows = []
    for t in tasks:
        mark = {
            "completed": "✅", "waiting_for_input": "❓", "failed": "❌",
            "running": "⏳", "superseded": "🔁",
        }.get(t["stage"], "?")
        rows.append(
            f"{mark} {t['id']}  {t['skill']:<20} 产物={t['artifacts'] or '（无）'}"
            + (f"  引用={t['refs']}" if t.get("refs") else "")
        )
    _emit("task", f"（本轮耗时 {elapsed:.0f}s）\n" + "\n".join(rows))


def main() -> None:
    ap = argparse.ArgumentParser(description="交互式跑一个 case（用户自己当用户）")
    ap.add_argument("--case", required=True, help="case id（如 t2-same-turn）或 yaml 路径")
    ap.add_argument("--thread", default="interactive-1")
    args = ap.parse_args()

    case_path, case = _load_case(args.case)
    inst = install(case)
    graph = build()
    config = {"configurable": {"thread_id": args.thread}}

    import proto.agent

    tag = f"interactive-{case['id']}-{time.strftime('%H%M%S')}"
    out_dir = ROOT / "evals" / "runs" / tag
    out_dir.mkdir(parents=True, exist_ok=True)

    # CLI 输出流原样落一份（终端里滚过去的东西，文件里也留得住）
    global _SINK
    _SINK = (out_dir / "_console.log").open("w", encoding="utf-8")

    print(BAR)
    print(f"交互式跑 case：{case['id']}")
    print(f"技能：{', '.join(sorted(proto.agent.SKILLS.keys()))}")
    print(f"日志：{trace.LOG_FILE}")
    print(BAR)
    print("现在**你是用户**：想说什么就说什么（空行结束）。")
    print("提示：系统提问时，故意答非所问 / 只答一半，看它怎么办。\n")

    turns: list[dict] = []
    seen_len = 0
    trace_pos = [Path(trace.LOG_FILE).stat().st_size if Path(trace.LOG_FILE).exists() else 0]
    t0 = time.time()
    error = None
    try:
        while True:
            try:
                # 🔴 **提示符本身就是那行 gutter** —— 你打的字直接接在后面，天然是一行。
                # 原来是 `input("你 › ")` 再 `_emit("user", ...)`，于是同一句话出现两次
                # （终端回显一次、播报一次），用户 2026-10-30 指出："没必要显示两条啊"。
                icon, color = _STYLE["user"]
                pad = " " * max(0, _LABEL_W - _dw(icon))
                prompt = f"[{time.strftime('%H:%M:%S')}] {icon}{pad}┃ "
                text = input(paint(prompt, color)).strip()
                _to_file(prompt + text)
                # 管道输入没有终端回显，补上（不然这行是空的）；真终端上**不要**补，
                # 否则同一句话出现两次 —— 那正是用户 2026-10-30 指出的毛病。
                if text and not sys.stdout.isatty():
                    print(text)
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not text:
                break
            t1 = time.time()
            # 🔴 `graph.invoke` 是**阻塞**的（一轮 2–3 分钟），所以另起一个线程
            # **实时 tail trace 日志**把过程推出来 —— 见 `_tail_trace` 的注释。
            stop = threading.Event()
            tailer = threading.Thread(target=_tail_trace, args=(stop, trace_pos), daemon=True)
            tailer.start()
            try:
                state = graph.invoke({"messages": [HumanMessage(text)], "log": []}, config)
            finally:
                stop.set()
                tailer.join(timeout=2)
                # 🔴 **收尾再读一遍**：主 agent 的消息轨迹是这一轮**最后**写进日志的，
                # 不补这一下，它就被漏掉（表现为"主 agent 的话只在回合小结里出现、且顺序错"）。
                _drain_once(trace_pos)
            snap = snapshot(state, text, seen_len, len(turns) + 1, round(time.time() - t1, 1))
            snap["input_source"] = "human"  # 这句是用户当场打的，不是脚本预置
            turns.append(snap)
            seen_len = len(state.get("messages") or [])
            _turn_summary(state, time.time() - t1)
            print()
    except Exception as e:  # noqa: BLE001
        import traceback

        error = f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"
        print(f"\n⚠️ 炸了：{error[:500]}")

    result = {
        "case": case["id"],
        "rep": 1,
        "turns": turns,
        "turns_done": len(turns),
        "usage": inst["counter"].snapshot(),
        "artifact_writes": inst["writes"],
        "run_tasks": inst["run_tasks"],
        "artifact_ids": list(ARTIFACTS.keys()),
        "artifact_final": {
            k: {
                "title": v["title"],
                "summary": v.get("summary", ""),
                "chars": len(v["content"]),
                "content": v["content"],
            }
            for k, v in ARTIFACTS.items()
        },
        "fault": inst["fault"],
        "error": error,
        "wall_s": round(time.time() - t0, 1),
        "trace_log": str(trace.LOG_FILE),
        "case_file": str(case_path),
    }
    dump_artifacts(out_dir, case["id"], 1, result)
    dump_run_record(out_dir, case["id"], 1, result)
    # 把这次跑的**原始 trace** 作为附录并进 `_run.md`（跑批那条路径是 run.py 干的，交互式得自己做）
    from evals.run import _append_trace

    _append_trace(result, tag)
    if _SINK is not None:
        _SINK.close()
    (out_dir / "results.json").write_text(
        json.dumps([result], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    trace.close()
    record = out_dir / "artifacts" / f"{case['id']}__r1" / "_run.md"
    print(f"\n记录：{record}")


if __name__ == "__main__":
    main()
