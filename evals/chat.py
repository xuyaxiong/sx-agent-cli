"""交互式跑原型 —— **你当用户**，技能从一个文件夹全量注入。

    uv run python -m evals.chat                      # 默认 evals/skills/
    uv run python -m evals.chat --skills 别的目录

与 `evals.run`（跑批）的分工：
- **跑批**：case 预置 `turns` + 断言，机器判、出通过率 —— 验"稳不稳"
- **这里**：你自由对话，问题当场答 —— 验"真实节奏下能不能跑"，也用来手工试新技能

技能**启动时全量注入**（文件夹下每个 `*.md` 一个技能，须有 frontmatter `name`/`description`），
但主 agent 只看到 `name + description`（渐进式披露第一层），全文按需 `read_skill` 取。

跑完落一份与跑批**同格式**的记录：`runs/<tag>/`，含
`_console.log`（这屏的全文，去色）、`artifacts/<tag>__r1/_run.md`（逐轮全记录 +
软判断留档 + 原始 trace 附录）、产物原文与 `results.json`。
"""

# ruff: noqa: T201  # 交互式入口，输出即界面

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import unicodedata
import threading
from pathlib import Path

import yaml
from langchain_core.messages import AIMessage, HumanMessage

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from evals.harness import (  # noqa: E402
    DEFAULT_SKILLS_DIR,
    RUNS_DIR,
    _RECEIPT_KINDS,
    _split_receipt,
    ctx_warn,
    dump_artifacts,
    dump_run_record,
    ensure_app_dirs,
    install,
    snapshot,
)
from proto import artifacts as proto_artifacts  # noqa: E402
from proto import trace  # noqa: E402
from proto.graph import build  # noqa: E402
from proto.artifacts import ARTIFACTS  # noqa: E402

# ⚠️ 路径一律走 `harness`（`ROOT` / `RUNS_DIR` / `DEFAULT_SKILLS_DIR`）——
# 这里不再自己算，否则打包后会算出解包目录，和 exe 旁边那个目录对不上。
BAR = "─" * 74

#: 打这些就退出（大小写不敏感）—— 和 Ctrl-C / Ctrl-D 一样。
#: ⚠️ **空行不是退出**：原来空行就退出，一个多余的回车能把整场会话结束掉（见主循环的注释）。
_EXIT_WORDS = frozenset({"exit", "quit", "退出", "再见"})

#: 开场白里的标题 —— **产品名**（用户 2026-10-30：*"我这不是 ai 助教 我是 SX Agent"*）。
#: 面向用户，不是面向开发：所以不叫"原型"，也不按技能内容起名。
APP_TITLE = "SX Agent"


def _cut(s: str, width: int) -> str:
    """按**显示宽度**截断（CJK 占 2 列），超了补 `…`。"""
    if _dw(s) <= width:
        return s
    out, w = "", 0
    for ch in s:
        cw = _dw(ch)
        if w + cw > width - 1:
            break
        out += ch
        w += cw
    return out + "…"


#: 能力清单的配色：**名字亮、说明暗**（用户 2026-10-30：*"名字可以亮一些 描述暗一些
#: 搞些颜色差别啊"*）。非 tty / `NO_COLOR` 下 `paint()` 自动原样返回。
_SKILL_NAME_COLOR = "\033[97m"   # 亮白：名字
_SKILL_DESC_COLOR = "\033[90m"   # 暗灰：一句话说明


def _print_skills(skills, indent: str = "   ") -> None:
    """打能力清单：**名字一行、说明一行**，名字亮、说明暗。

    用户 2026-10-30：*"名字 和描述 换个行嘛 你直接显示太丑了"* ——
    原来挤在一行（`· AI 备课　根据学科、年级、课题生成教案`），中英混排一长就糊成一片。
    """
    for s in skills:
        head, detail = _skill_label(s)
        print(f"{indent}· {paint(head, _SKILL_NAME_COLOR)}")
        if detail:
            print(f"{indent}  {paint(detail, _SKILL_DESC_COLOR)}")


def _skill_label(skill) -> tuple[str, str]:
    """技能 →（**一句话能干什么**，细节）。

    按 `description` 的**第一个 `：`** 切 —— 技能作者本来就是这么写的
    （"AI 备课：根据学科、年级、课题生成教案"）。**不写死任何技能名**：
    用户自己加的技能也走这条，没有 `：` 就退回用 `name`。

    细节截到 68 列：开场白是给人扫一眼的，不是技能说明书。
    """
    desc = (skill.description or "").strip()
    head, sep, tail = desc.partition("：")
    if sep and head.strip() and tail.strip():
        return head.strip(), _cut(tail.strip(), 68)
    return skill.name, _cut(desc, 68)


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
    "ctx":    ("📊 CONTEXT",  "\033[90m"),
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


#: **正文层**（亮色）—— 只有"人和主 agent 的对话"。
#: 其余全部是**机制层**（暗灰）：执行器的话、工具调用、判定、注入……
#: 🔴 用户 2026-10-30 定：**两层靠颜色分，左边一律对齐**（原来机制层带缩进 + 🔩 图标）——
#: *"那个缩进还是不缩进了 对齐吧 反正颜色不一样能区分"*。
_LABEL_W = 12   # gutter 里标签的宽度（按显示列算；最长的 EXECUTOR/PLATFORM 是 11 列）
_TS = "[00:00:00.000] "   # gutter 宽度占位（显示只到时间，日期省掉）

#: 播报里已经打过的"执行器的话"（归一化前 60 字）—— 去重用。
#: 不去重的话，同一段话会以 🗣 EXECUTOR（实时）和 🧠 AGENT（小结）出现两次。
_SHOWN_EXEC: set[str] = set()


def _norm(s: str) -> str:
    return " ".join(s.split())[:60]


#: **播报层**里唯二会被截断的工具返回 —— 判据是"**全文另有出处**"：
#: 技能原文在 `evals/skills/*.md`，产物正文在产物文件里。
#: 🔴 **其余一律不截**（平台注入 / agent 的话 / 别的工具返回）：
#: 那些是**优化 agent 表现的观察窗口**，截了就观察不到（用户 2026-10-30 定）。
_CAPPED_TOOLS = ("read_skill", "read_artifact")
_CAP_TOOL = 250

_MAIN_ROLES = frozenset({"user", "agent"})
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


def _short_ts(stamp: str) -> str:
    """`2026-10-30 17:03:48.301` → `17:03:48.301` —— **显示层省掉日期**。

    用户 2026-10-30：*"加上日期太占用地方了 还是去掉只保留时间吧"*。
    ⚠️ 只改**显示**：trace 日志和 `_run.md` 里仍是完整时间戳（那是留档，跨天要能分）。
    """
    return stamp[11:] if len(stamp) >= 19 and stamp[10] == " " else stamp


def _wrap(s: str, width: int) -> list[str]:
    """按**显示宽度**折行（CJK 占 2 列）。

    🔴 **为什么必须自己折**（2026-10-30 用户提的：*"我们能处理这种行太长的显示问题吗"*）：
    交给终端折的话，**续行会顶到最左边** —— `┃` 那道 gutter 就断了，
    读的人分不清"这半句是上面那条的续行"还是"另一条消息"。
    自己折，续行就能**带上同一道 gutter**（`_emit` 的 `cont`）。

    宽度按 `_dw` 算，和 gutter 对齐用的是同一套口径（见 `_dw` 的注释）。
    """
    if width <= 1:
        return [s]
    out: list[str] = []
    cur, w = "", 0
    for ch in s:
        cw = _dw(ch)
        if w + cw > width and cur:
            out.append(cur)
            cur, w = "", 0
        cur += ch
        w += cw
    out.append(cur)
    return out


def _emit(role: str, text: str, stamp: str | None = None) -> None:
    """按统一版式打一条：`[时间] 标签 ┃ 内容`，续行对齐到 `┃`。

    🔴 **两层靠颜色分，不靠缩进**（用户 2026-10-30：*"那个缩进还是不缩进了 对齐吧
    反正颜色不一样能区分"*）：正文层亮色、机制层暗灰，**左边一律对齐**。
    """
    icon, color = _STYLE[role]
    stamp = _short_ts(stamp or trace.stamp())
    pad = " " * max(0, _LABEL_W - _dw(icon))
    head = f"[{stamp}] {icon}{pad}┃ "
    indent = _dw(_TS) + _LABEL_W
    tint = color if role in _MAIN_ROLES else _MECH_COLOR
    cont = " " * indent + "┃ "
    # 折行宽度 = 终端列数 − gutter（`┃ ` 前面那截）—— 自己折，续行才带得上 gutter。
    # ⚠️ 管道运行（没有 tty）时 `get_terminal_size()` 会退到 fallback —— 用 100 而不是默认的 80：
    # 这个宽度也决定 `_console.log` 怎么折，80 会把记录折得过窄。
    avail = shutil.get_terminal_size(fallback=(100, 30)).columns - indent - 2
    lines = [
        ln for raw in ((text or "").splitlines() or [""]) for ln in _wrap(raw, avail)
    ]
    print(paint(head + lines[0], tint))
    _to_file(head + lines[0])
    for ln in lines[1:]:
        print(paint(cont + ln, tint))
        _to_file(cont + ln)


def _live(role: str, line: str) -> None:
    """把一条 trace 日志行推出去 —— 时间戳**用日志里的**（那才是事件真实发生的时刻）。"""
    m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]\s*(.*)", line, re.S)
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
    m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]\s?(.*)", line, re.S)
    if m:
        st["ts"] = m.group(1)
        body = m.group(2)
        if body.startswith(trace.STREAM_MARK):
            # 🔴 **模型正在写的那一行**（`trace.stream_agent` 攒到换行就写一行）。
            # 这是"看着它干活"的唯一来源 —— 一次 LLM 调用那几十秒，别的什么都没有。
            who, _, text = body[len(trace.STREAM_MARK) :].partition("⟩ ")
            _emit("say" if who.startswith("执行器") else "agent", text, stamp=st["ts"])
            return
        if st["block"] is not None:
            # ⚠️ `PROMPT（…）` / `RESPONSE（…）` 这两行**自带新时间戳**，但它们属于**同一个事件**
            # （`safe_structured` 先 section 再 block，两行都带 [ts]）——
            # 一律当"新事件"处理的话，块刚开就被冲掉，内容全丢（实测踩过）。
            if body.startswith(("PROMPT（", "RESPONSE（")):
                st["block"][1].append(body)
                return
            # 🔴 **整块照打，不截断**：这些块是**平台注入**（交接书 / 台账）和
            # **LLM 决策**（判定 / 试答）—— 用户点名"千万不要截断"的那类。
            _emit(st["block"][0], "\n".join(st["block"][1]), stamp=st["block"][2])
            st["block"] = None

        if "消息轨迹" in body:  # 段落头：接下来的续行是谁的 dump
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

    # ── 续行：要么在多行块里，要么在某个 dump 里（trace 日志里没有时间戳前缀的行）──
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
        # ⚠️ **不截参数**：`delegate(task=...)` 就是交接本身，砍了等于看不到交接了什么
        # （2026-10-30 用户：*"你怎么把这么关键的上下文交接信息给去掉了？？"*）。
        # 太长由 `_emit` 自己折行（续行带 gutter），不需要在这里砍。
        _emit("tool", f"{who} → {name}（{args}）", stamp=st["ts"])
        return
    if s.startswith("content="):
        # 日志里是 Python repr（单行、转义过），这里还原成人看的样子：
        # `\n` → 换行；`\\` → 单个反斜杠（**Windows 路径全靠这条** —— 不还原的话
        # 用户看到的是 `C:\\Users\\...`，没法直接拷进资源管理器）。
        # ⚠️ 顺序不能反：先还原 `\n`，再还原 `\\`；否则 `\\n`（字面反斜杠+n）会被拆错。
        body = (
            s[len("content=") :].strip().strip("'")
            .replace(chr(92) + "n", "\n")
            .replace(chr(92) * 2, chr(92))
        )
        if st["kind"] == "ToolMessage":  # 工具**返回了什么**
            who = st["calls"].pop(0) if st["calls"] else "?"
            # 🔴 **只有这两种工具返回截断**：它们的全文**另有出处**
            # （技能原文在 `evals/skills/*.md`，产物正文在产物文件里），
            # 摊在终端里只是噪音。**其余一律不截** —— 见下面两条。
            if who in _CAPPED_TOOLS and len(body) > _CAP_TOOL:
                body = body[:_CAP_TOOL] + f"（后面还有 {len(body) - _CAP_TOOL} 字未显示）"
            _emit("ret", f"{who} 的返回：\n{body}", stamp=st["ts"])
        elif st["kind"] == "SystemMessage":  # 平台注入的台账
            # 🔴 **不截断**：平台注入是"优化 agent 表现"的观察窗口
            # （用户 2026-10-30：*"如果没有这些东西观察不到"*）
            _emit("inject", f"平台 → 主 agent（每轮注入的任务台账）：\n{body}", stamp=st["ts"])
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


def _turn_summary(snap: dict, elapsed: float) -> None:
    """回合小结：**台账 + 主上下文读数**。

    人和主 agent 的话、工具调用、判定、投递……全都已在**实时播报**（`_tail_trace`）里
    按真实时序打过了 —— 这里再打一遍就会重复，而且顺序是错的
    （小结在整轮跑完之后才执行）。用户 2026-10-30 指出："包括时序这些"。

    🔴 **主上下文大小每轮都打**（用户 2026-10-30 要求："在我们 cli 里面也显示一下
    这个重要信息"）—— 它是**隔离效果的唯一现场读数**：产物几千上万字、主上下文该只有
    几千字；这个数一旦跟着产物一起涨，就是隔离漏了（产物正文被读进了主上下文）。
    ⚠️ **只记录、不判红**（用户同日定）：读产物是主 agent 的**合法能力**，
    不是违规 —— 这里给的是读数，让读的人自己看出代价。
    ⚠️ 口径与跑批一致（`harness.snapshot`）：`main_ctx_chars` **不含 system prompt**，
    所以两个数都给出来，真实上下文是两者相加。
    """
    ctx = snap.get("main_ctx_chars") or 0
    sys_chars = snap.get("sys_prompt_chars") or 0
    body = snap.get("artifacts_chars") or 0
    warn = ctx_warn(ctx + sys_chars)
    _emit(
        "ctx",
        f"主上下文 {ctx + sys_chars} 字（消息 {ctx} + system {sys_chars}）"
        f"　｜　产物库正文 {body} 字　｜　本轮耗时 {elapsed:.0f}s"
        + (f"\n{warn}" if warn else ""),
    )

    tasks = snap.get("tasks") or []
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
    _emit("task", "\n".join(rows))


def main() -> None:
    ap = argparse.ArgumentParser(description="交互式跑原型（你当用户；技能从文件夹加载）")
    ap.add_argument(
        "--skills",
        default=str(DEFAULT_SKILLS_DIR),
        help=f"技能包目录（默认 {DEFAULT_SKILLS_DIR}）：**每个子目录是一个技能包**，"
        "入口是 SKILL.md（frontmatter 给 name/description，正文即 instructions），"
        "包里的文本文件（references/ assets/ …随作者）按需读（渐进式披露第三层，read_skill_file）",
    )
    ap.add_argument("--thread", default="interactive-1")
    args = ap.parse_args()

    # 首次运行（只在打包后）：建 skills/ 和 runs/，空的话放一遍示例技能。
    # ⚠️ 返回的是"做了什么"（建目录 / 放技能），但**只有"放了技能"值得告诉用户** ——
    # "建目录"是内部事务，说了只是噪音。措辞也换成人话（不提目录名）。
    created = ensure_app_dirs()
    # ⚠️ 数字**由 `ensure_app_dirs` 报**，别写死 —— 原来硬编码"3 个"，
    # 而 `evals/skills/` 里早就是 5 个包了（2026-11-02 空跑时发现：嘴上说 3、实际铺 5）。
    seed_note = next((c for c in created if "示例技能" in c), "")

    # 技能来自**文件夹**（不再绑 case）—— 用户 2026-10-30：*"有一个文件夹里面有各种 skill
    # cli 启动时候自动注入文件夹里面的全部 skill"*。case 那套是跑批用的（预置 turns + 断言），
    # 这个入口是给人自由对话用的。
    inst = install({"skills_dir": args.skills, "skills_mode": "replace"})
    graph = build()
    config = {"configurable": {"thread_id": args.thread}}

    import proto.agent
    from proto.config import describe

    label = Path(args.skills).name or "skills"
    tag = f"interactive-{label}-{time.strftime('%m%d-%H%M%S')}"
    # 记录落 `harness.RUNS_DIR`：开发时是仓库里的 `evals/runs/`，
    # 打包后是 **exe 旁边的 `runs/`** —— 用户找得到，也方便整包发回来。
    out_dir = RUNS_DIR / tag
    out_dir.mkdir(parents=True, exist_ok=True)

    # CLI 输出流原样落一份（终端里滚过去的东西，文件里也留得住）
    global _SINK
    _SINK = (out_dir / "_console.log").open("w", encoding="utf-8")

    # 🔴 **产物当场落盘**（2026-10-30 用户提的：*"保存地址要说一下 不然不清楚哪里找去"*）——
    # 告诉产物库"这次 run 的产物放哪"，`store_artifact` 写完就落盘，
    # 于是 `write_artifact` 的返回里能给出**真实路径**，用户当场就能打开看。
    # 落点和最后 `dump_artifacts` 的目录**必须一致**，否则前后两份文件对不上。
    # ⚠️ 走函数而不是直接赋值 —— 写错模块会当场 AttributeError（见 `use_dir` 的 docstring）
    proto_artifacts.use_dir(out_dir / "artifacts" / f"{tag}__r1")

    skills = list(proto.agent.SKILLS.values())
    # 🔴 **开场白只有一套**（用户 2026-11-02：*"合成一套啊 以开发态为主"*）——
    # 原来按 `FROZEN` 分叉成"用户版 / 开发版"两份文案：同一个屏幕上看过两遍，
    # 改一句话还得改两处。留开发态这版。
    # ⚠️ 10-30 曾把路径当"用户版里的冗余"删掉（*"类似这种信息不要加了啊 太冗余了"*）；
    # 现在**两边都留着** —— `记录` 那个文件夹打包后正是要用户发回来的东西。
    print(BAR)
    # 模型只显示**名字**（端点 URL 在 `.env` 里，打在这里只是噪音 —— 用户 2026-10-30 定）。
    print(f"{APP_TITLE} ｜ 模型 {describe().split(' @ ')[0]}")
    # 标题自己占一块（用户 2026-11-02：*"技能清单这一行上面也加一个横线分隔"*）——
    # 原来标题下面直接顶"技能清单"，扫一眼分不出哪行是"这是谁"、哪行是"有什么"。
    print(BAR)
    print("技能清单")
    _print_skills(skills, indent="   ")
    print(BAR)
    print(f"日志   {trace.LOG_FILE}")
    print(f"记录   {out_dir}")
    print(BAR)
    print("直接说你要什么。退出：Ctrl-C 或 exit。\n")
    if seed_note:
        # 只在打包后首次运行有（开发态目录本来就在）—— 用户得知道技能从哪来。
        print(f"  {seed_note} —— 你也可以自己往 skills 文件夹里加。\n")

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
                prompt = f"[{_short_ts(trace.stamp())}] {icon}{pad}┃ "
                text = input(paint(prompt, color)).strip()
                _to_file(prompt + text)
                # 管道输入没有终端回显，补上（不然这行是空的）；真终端上**不要**补，
                # 否则同一句话出现两次 —— 那正是用户 2026-10-30 指出的毛病。
                # ⚠️ 空行也要打（`print("")` = 只换行）：不然下一个提示符会接在同一行上。
                if not sys.stdout.isatty():
                    print(text)
            except (EOFError, KeyboardInterrupt):
                print()
                break
            # 🔴 **空行不再退出**（2026-10-30 改）：原来 `if not text: break`，
            # 于是**一个多余的回车就把整场会话结束掉**，而且**一声不吭** ——
            # 用户实测踩到：*"运行过程中突然进程退出了"*（第 5 轮答完，提示符刚出现就没了）。
            # 触发很常见：粘贴的内容带尾随换行，那个换行留在缓冲里，
            # 下一个提示符一出现就被当成"空行"吃掉。
            # **退出改成显式动作**：Ctrl-C / Ctrl-D，或打 `exit` / `quit`。
            if not text:
                continue
            if text.lower() in _EXIT_WORDS:
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
            _turn_summary(snap, time.time() - t1)
            print()
    except Exception as e:  # noqa: BLE001
        import traceback

        error = f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"
        print(f"\n⚠️ 炸了：{error[:500]}")

    result = {
        "case": tag,
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
                #: 产出方**声明的**类型（`md` / `html`）—— 断言按它判"哪些算网页"，
                #: 不按内容嗅探（见 `checks.check_artifact_html_complete`）
                "type": v.get("type", ""),
                "ext": v.get("ext", ""),
                "chars": len(v["content"]),
                "content": v["content"],
            }
            for k, v in ARTIFACTS.items()
        },
        "fault": inst["fault"],
        "error": error,
        "wall_s": round(time.time() - t0, 1),
        "trace_log": str(trace.LOG_FILE),
        "skills_dir": str(args.skills),
    }
    dump_artifacts(out_dir, tag, 1, result)
    dump_run_record(out_dir, tag, 1, result)
    # 把这次跑的**原始 trace** 作为附录并进 `_run.md`（跑批那条路径是 run.py 干的，交互式得自己做）
    from evals.run import _append_trace

    _append_trace(result, tag)
    if _SINK is not None:
        _SINK.close()
    (out_dir / "results.json").write_text(
        json.dumps([result], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    trace.close()
    # 收尾**也只有一套**（用户 2026-11-02：*"结尾也统一起来 这个以打包为主"*）——
    # 留打包那版：**说清记录在哪 + 请对方发回来**。收尾才说路径不唐突
    # （刚做完事，这时候说"把它发我"才自然）；开场就说才叫噪音。
    # **回收是功能，不是附注。**
    # 🔴 **退出必须说一声**（2026-10-30）：原来一声不吭就结束，
    # 用户看到的就是"进程突然没了" —— 分不清是正常结束还是崩了。
    print(f"\n  再见 —— 这次对话已记录，请把这个文件夹发给我：\n    {out_dir}\n")


if __name__ == "__main__":
    main()
