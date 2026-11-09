"""交互式跑原型（**产品入口**，打包打的就是它）—— **你当用户**，技能从一个文件夹全量注入。

    uv run python -m app.chat                      # 默认 skills/
    uv run python -m app.chat --skills 别的目录

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
import asyncio
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from pathlib import Path

from langchain_core.messages import HumanMessage

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.runtime import (  # noqa: E402
    DEFAULT_SKILLS_DIR,
    RUNS_DIR,
    ctx_warn,
    dump_artifacts,
    dump_run_record,
    ensure_app_dirs,
    install,
    snapshot,
)
from proto import aio
from proto import recorder, session, trace  # noqa: E402
from proto.graph import build, next_pending  # noqa: E402
from proto.handoff.briefing import asked_text  # noqa: E402
from proto import artifacts as artifacts_mod  # noqa: E402 —— 全仓只留**一个**别名
from proto.registry import registry  # noqa: E402

# ⚠️ 路径一律走 `harness`（`ROOT` / `RUNS_DIR` / `DEFAULT_SKILLS_DIR`）——
# 这里不再自己算，否则打包后会算出解包目录，和 exe 旁边那个目录对不上。
BAR = "─" * 74

#: 打这些就退出（大小写不敏感）—— 和 Ctrl-C / Ctrl-D 一样。
#: ⚠️ **空行不是退出**：原来空行就退出，一个多余的回车能把整场会话结束掉（见主循环的注释）。
_EXIT_WORDS = frozenset({"exit", "quit", "退出", "再见"})

#: 在待答卡片下打这些 = **显式跳过**（大小写不敏感）。
#: ⚠️ **跳过必须是一个"打出来"的动作** —— 见 `_read_card_answer` 的 docstring。
_SKIP_WORDS = frozenset({"skip", "跳过"})

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


#: 技能清单的配色：**名字亮、说明暗**（用户 2026-10-30：*"名字可以亮一些 描述暗一些
#: 搞些颜色差别啊"*）。非 tty / `NO_COLOR` 下 `paint()` 自动原样返回。
_SKILL_NAME_COLOR = "\033[97m"   # 亮白：名字
_SKILL_DESC_COLOR = "\033[90m"   # 暗灰：一句话说明


def _print_skills(skills, indent: str = "   ") -> None:
    """打技能清单：**名字一行、说明一行**，名字亮、说明暗。

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
    "exec":   ("🚀 RUN",      "\033[94m"),   # 亮蓝
    "ask":    ("💬 ASK",      "\033[91m"),   # 亮红
    "plat":   ("📮 PLATFORM", "\033[90m"),   # 灰
    "done":   ("🏁 DONE",     "\033[92m"),   # 亮绿
    "task":   ("📋 TASKS",    "\033[90m"),
    "ctx":    ("📊 CONTEXT",  "\033[90m"),
}

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

# （旧 `_LIVE` 子串匹配表已删 —— 2026-11-03 起角色归属改由事件的 `kind` 语义标签决定，
#   见下面的 `_ROLE_BY_KIND`。按子串认行的失配风险（措辞一改就静默丢块）随之作废。）


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
#: 其余全部是**机制层**（暗灰）：执行器的话、工具调用、试答、注入……
#: 🔴 用户 2026-10-30 定：**两层靠颜色分，左边一律对齐**（原来机制层带缩进 + 🔩 图标）——
#: *"那个缩进还是不缩进了 对齐吧 反正颜色不一样能区分"*。
_LABEL_W = 12   # gutter 里标签的宽度（按显示列算；最长的 EXECUTOR/PLATFORM 是 11 列）
_TS = "[00:00:00.000] "   # gutter 宽度占位（显示只到时间，日期省掉）

#: **读全文的工具** —— 它们的返回**一个字都不贴**，只报"读了多大一份、原文在哪"。
#:
#: 🔴 用户 2026-11-03：*"读取 skill 全文 你全显示没必要"*。
#: 判据还是那条老的：**全文另有出处**（技能原文在技能包里，产物正文在产物落盘文件里）——
#: 而"另有出处"的正文，贴一段（原来是 250 字）**既不是全文、又不是摘要**，纯粹是噪音。
#: 同 `harness._render_messages` 的做法（"读了 X 的全文（N 字，原文不在这里复述）"）。
#:
#: 🔴 **其余一律不贴不行、也不截**（平台注入 / agent 的话 / 别的工具返回）：
#: 那些是**优化 agent 表现的观察窗口**，截了就观察不到（用户 2026-10-30 定）。
#:
#: ⚠️ **记录层不跟着改**：`logs/proto-*.log` 是**原始记录**，照旧全文
#: （`trace.render_message` 的契约：*"要少看噪音就在播报层砍，别在记录层砍"*）。
_READ_TOOLS: dict[str, str] = {
    "read_skill": "技能包",
    "read_skill_file": "技能包",   # 渐进式披露第三层：技能自带的 references/ assets/
    "read_artifact": "产物落盘文件",
}
#: ⚠️ **`read_user_turn` 不在此列**：它返回的是**用户原话**（事实来源），
#: 不是"另有一份文件的大块正文" —— 而且它正是【用户原话】那段的**渐进式披露出口**，
#: 贴出来才知道执行器到底读到了什么。

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


#: 折行的**最小单位**：英文词 / 标识符 / `` `代码段` `` 各是一整块，其余逐字。
#:
#: 🔴 **为什么不能逐字符折**（用户 2026-11-03 实测看到）：`read_user_turn(轮次)`
#: 被折成 `read_us` / `er_turn`，`waiting_for_input` 折成 `waiting_for_inpu` / `t` ——
#: 读的人得自己拼，**拷贝的人拼不回来**（而日志里的标识符正是拿来搜代码的）。
#: CJK 逐字断没问题（中文本来就能在字间断），英文词和标识符不行。
#:
#: ⚠️ 三个分支各有来由，别简化成一个 `\w+`：
#:   - `` `…` ``：反引号是**成对**的，拆开就成了 `` …就用 ` `` / `` read_user_turn(轮次)` 读… ``
#:     （2026-11-03 实测看到）
#:   - `[0-9A-Za-z_]+(?:\.[0-9A-Za-z_]+)*`：带点的标识符/文件名要连上 ——
#:     否则 `SKILL.md` 从点处断成 `SKILL.` / `md`
#:   - `[\s\S]`：其余**逐字**（CJK 本来就该在字间断）
_TOKENS = re.compile(r"`[^`]+`|[0-9A-Za-z_]+(?:\.[0-9A-Za-z_]+)*|[\s\S]")


def _wrap(s: str, width: int) -> list[str]:
    """按**显示宽度**折行（CJK 占 2 列），且**优先断在词边界上**。

    🔴 **为什么必须自己折**（2026-10-30 用户提的：*"我们能处理这种行太长的显示问题吗"*）：
    交给终端折的话，**续行会顶到最左边** —— `┃` 那道 gutter 就断了，
    读的人分不清"这半句是上面那条的续行"还是"另一条消息"。
    自己折，续行就能**带上同一道 gutter**（`_emit` 的 `cont`）。

    宽度按 `_dw` 算，和 gutter 对齐用的是同一套口径（见 `_dw` 的注释）。

    🔴 **续行要补回行首缩进**（2026-11-03 实测踩到）：段体缩进（`handoff.BODY`）是**结构**
    —— 段名 0 列、段体 2 列、每层 +2。折行不补缩进的话，一条长段体会被折成
    "第一行缩进、后面几行顶到 0 列"，**结构在终端里就断了**，读的人分不清
    哪几行属于哪一段（实测：`【任务】` 的正文第二行掉到 0 列，看着像另一段）。
    ⚠️ 所以 `"".join(_wrap(s, w))` 对**带缩进的行**不再等于 `s` —— 缩进是**故意重复**的，
    内容一个字不丢（自检见 `unit_decision` 的同源断言）。
    """
    if width <= 1:
        return [s]
    lead = s[: len(s) - len(s.lstrip())]
    body = s[len(lead) :]
    if not body:
        return [s]
    inner = max(1, width - sum(_dw(c) for c in lead))
    out: list[str] = []
    cur, w = "", 0
    for tok in _TOKENS.findall(body):
        tw = sum(_dw(c) for c in tok)
        if w + tw > inner and cur:
            # 整块放不下 → 换行。**不留半截词**：下一行的开头就是这个完整 token。
            out.append(lead + cur)
            cur, w = "", 0
        if tw > inner:
            # 单个词比整行还宽（超长 URL / 标识符）—— 没有边界可用，只能硬切
            for c in tok:
                if w + _dw(c) > inner:
                    out.append(lead + cur)
                    cur, w = "", 0
                cur += c
                w += _dw(c)
            continue
        cur += tok
        w += tw
    out.append(lead + cur)
    return out


def _cap_line(text: str) -> str:
    """机制层摘要行的截断 —— **切了必须标**（交接三问②）。

    🔴 单独成函数是为了**只有一个来源**：`_emit` 用它，单测的捕获器也用它。
    原来是内联在 `_emit` 里，测试的 stub 重写了一遍 `_emit` 就把这段丢了 ——
    于是「超长行带截断标记」那条断言测的是 stub 自己，**永远是红的**（2026-11-03 发现）。
    """
    if len(text) <= _LIVE_CAP:
        return text
    return text[:_LIVE_CAP] + f"（**平台截断：还有 {len(text) - _LIVE_CAP} 字未显示**）"


def _emit(role: str, text: str, stamp: str | None = None, *, cap: bool = False) -> None:
    """按统一版式打一条：`[时间] 标签 ┃ 内容`，续行对齐到 `┃`。

    🔴 **两层靠颜色分，不靠缩进**（用户 2026-10-30：*"那个缩进还是不缩进了 对齐吧
    反正颜色不一样能区分"*）：正文层亮色、机制层暗灰，**左边一律对齐**。

    `cap=True`：机制层摘要行超过 `_LIVE_CAP` 带**标记**截断（切了不标是最糟的）；
    决策块 / 注入块**永不截**（那是观察窗口）。
    """
    if cap:
        text = _cap_line(text)
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


# ──────────────────────────── 事件订阅：展示层只认结构化事件 ────────────────────────────
#
# 🔴 **契约是事件，不是文本**（2026-11-03 架构定）：本层**订阅** `proto.trace` 的事件总线，
# 拿到的是结构化事件（谁 → 谁、字段是什么、消息对象本身）——**不再 tail 日志文件、
# 不再按字符串前缀认行、不再做 repr 反转义**。前缀 / 缩进 / gutter 是本层的排版决策，
# 记录层不知道它们存在。
# （旧做法等于展示层对记录层的输出做**反结构化**——正是 `proto/handoff` 那条铁律禁止的事；
#  2026-11-02 harness 手抄段名失配的事故，结构上就是它。旧的三条 hack 随之作废：
#  repr 反转义、跨段 FIFO 配对工具名、按子串认块——`tool_call_id` 在消息对象里本来就有。）

#: 业务语义标签 → 显示角色。记录层只发"发生了什么"（`kind`），**角色归属是本层的决定**。
_ROLE_BY_KIND: dict[str, str] = {
    "dispatch": "plat",         # delegate 委派
    "supersede": "plat",        # 取代旧 Task
    "delegate_failed": "plat",  # 委派以 failed 收场
    "run_begin": "exec",        # run_task 开始（section）
    "run_end": "exec",          # run_task 结束
    "feed": "plat",             # 平台把用户原话投给待答任务（section）
    "retry": "plat",            # 这一轮抖动、已重试一次（trace.stream_agent）
    "stream_empty": "plat",     # 流式一步都没吐 → 退回 invoke 兜底（这一趟没有流式日志）
    "run_fault": "exec",        # 重试过仍失败 → 这一轮没跑成（executor.run_task）
    "ask": "exec",              # 执行器申报要信息（调了 `ask_user`）→ 这一轮结束
    "delivered": "exec",        # 执行器交付了产物 → 这一轮结束
}

#: 有**专门渲染分支**的 kind（不走上表那条"摘要行"的路）。
_SPECIAL_KINDS = frozenset({
    "ask_main",            # 试答：开一个挂着的块，PROMPT/RESPONSE 归并进来
    "executor_briefing",   # 平台注入执行器的整份交接书（block）
    "waiting",             # 主 agent 答不上 → 登记待答（note）
    "relay",               # 待答透传（note）
    "llm_io",              # 那次 LLM 调用的 PROMPT/RESPONSE/失败提示
})

#: **故意不上屏**的 kind —— 只进日志文件。写在这里 = 一个**表态**（不是漏了）。
_LOG_ONLY_KINDS = frozenset({
    "nudge",   # 推执行器继续的提示词（正文已由 `handoff("推回去")` 记档）
})

# 🔴 三个集合的**并集必须覆盖 `trace.KINDS`**（`unit_console` ⑪ 守着）：
#    新加的 kind 必须在**这一层**当场表态 —— 上屏 / 有专门分支 / 只进日志。
#    漏表态的后果是那一行**静默消失**（2026-11-03 审核 F3）。

#: 挂着的块：`[role, lines, ts]` —— 只用于把同一次 LLM 调用的 PROMPT/RESPONSE 归并成一块。
_PENDING: list | None = None

#: 播报行的长度上限（超了带标记截断）—— 只对机制层摘要行生效，决策块 / 注入块不截。
_LIVE_CAP = 400


def _close_pending() -> None:
    """把挂着的块先吐出来，再播下一条事件。

    🔴 **整块照打，不截断**：这些块是**平台注入**（交接书 / 台账）和
    **LLM 决策**（试答）—— 用户点名"千万不要截断"的那类。
    但"整块"只指**内容**，不指**时机**：块没闭合就播下一条，
    读的人看到的因果就是倒的（2026-11-03 实测踩过：执行器都交付完了，
    "平台在注入任务"才姗姗播出）。
    """
    global _PENDING
    if _PENDING is not None:
        _emit(_PENDING[0], "\n".join(_PENDING[1]), stamp=_PENDING[2])
        _PENDING = None


def _render_decision(ev: dict, *, with_who: bool = True) -> str:
    """决策**事件** → 显示文本。

    🔴 **2026-11-05：渲染收敛到 `handoff.decision.render_decision`** ——
    在这之前**这一层和记录层各排一遍**，而且**排版还不一样**（记录层 4 格、这里 0/2 格）✗。
    同一件事两处定义，形状必然漂（"改一句文案、下游静默失效"那一族）。

    现在：**排版归一处**，本层只加**自己的载体**（段头一行 ＋ 整体缩 2 格，
    让它挂在段头下面）。

    `with_who=False`：**接在挂着的块尾巴上**时用（那个块的标题已经是同一句话了，
    见 `k == "ask_main"` 那段）—— 再印一遍就是同一个"谁"两副面孔的翻版。
    ⚠️ 那种情况下**也不再多缩 2 格**：缩进是为了"挂在段头那行下面"，
    而块里已经没有那一行了 —— 同块里 PROMPT（0 列）和四段（2 列）会看着像两层。
    """
    from proto.handoff.decision import render_decision

    body = render_decision(ev)
    if not with_who:
        return body
    return f"{ev['who']}\n" + "\n".join(f"  {ln}" for ln in body.splitlines())


#: 已经吼过的未知角色的值 —— 同一个错误只吼一次，别把屏幕刷了
_WARNED_ROLE: set[str] = set()

#: 同上：已经吼过的"没表过态的 kind"
_WARNED_KIND: set[str] = set()

#: 角色 → 屏幕上怎么称呼它。**角色是逻辑（枚举），这里是文案** —— 两回事。
_ROLE_LABEL = {trace.AGENT: "主 agent", trace.EXECUTOR: "执行器"}


def _role_of(ev: dict) -> str:
    """事件里的角色 —— **只认枚举值**（`trace.AGENT` / `trace.EXECUTOR`）。

    🔴 2026-11-03 e2e 实测暴露的最后一处耦合：原来这两处是靠
    `"执行器" in str(ev["who"])` / `"run_task" in str(ev["tag"])` 认角色的 ——
    而那是**措辞**。改个显示名（"执行器"→"执行者"）就**静默降级成主 agent 的角色**，
    没有报错、没有提示，只是颜色和前缀悄悄错了。

    现在 `role` 是事件里单独的字段。取不到 / 值不合法就**当场吼一声**（同一个值只吼一次），
    按"未知"上屏 —— 不猜、也不假装没发生。
    """
    role = ev.get("role")
    if role in trace.ROLES:
        return role
    key = repr(role)
    if key not in _WARNED_ROLE:
        _WARNED_ROLE.add(key)
        _emit("plat", f"⚠️ 收到未知角色的事件：role={role!r}（type={ev.get('type')}）"
                      f" —— 按「未知」上屏。合法值：{sorted(trace.ROLES)}",
              stamp=ev.get("ts"))
    return "unknown"


#: 工具配对表：**`(role, tool_call_id)` → 工具名**，跨事件累积（不是每个事件重置）。
#:
#: 🔴 2026-11-03 修的一个真回归：`dump` 是**按 super-step 分批**发的 ——
#: 主 agent 的 `tool_call` 落在第 1 步的 dump 里，它的 `ToolMessage`
#: 却落在第 **2** 步的 dump 里。原来 `by_id` 是 `_on_dump` 的**局部变量**，
#: 每个事件重建一次，于是跨事件那一半配不上，屏幕上是 `? 的返回：`。
#: 旧文本契约时代靠"跨段 FIFO 队列"猜着补，删掉它时没做等价替换（漏了）。
#:
#: 🔴 **为什么键里必须带 `role`**（2026-11-06 修 SX-12）：`tool_call_id` 只在**一次
#: 模型响应内**唯一 —— provider 每轮从 `call_0` 之类的号**重新发**（这里原来写着
#: "id 本身全局唯一"，是错的）。主 agent 和执行器是两个 agent，id 空间**会撞**：
#: 实测 pbl/zhiwo 各一次 —— 主 agent 的 `delegate`（call_0）先入表，执行器在它内部
#: 跑的那几分钟里 `read_skill_file` 也叫 call_0，**把映射覆盖了**；回执几分钟后
#: 才 dump，查表查到执行器的工具名（RETURN 行标成 `read_skill_file 的返回`）。
#: 带上角色后两个 agent 的映射互不覆盖；**同一角色内配对仍然成立**（主 agent 的调用
#: 和回执之间它自己被阻塞、没有别的 dump；执行器每轮整批 dump、消息序内配对）。
#:
#: ⚠️ **加个上限**（2026-11-04 复审 N5）：它只增不减，长会话会一直涨。
#: 配对**只发生在同一轮之内**（tool_call 与它的返回都在同一次 `graph.invoke` 里），
#: 所以到顶就整体清空 —— 丢的都是早就配完对的旧 id，不影响任何一次配对。
_TOOL_NAMES: dict[tuple[str, str], str] = {}
_TOOL_NAMES_CAP = 2000


def _on_dump(ev: dict) -> None:
    """消息轨迹事件 —— **消息对象本身就在事件里**。

    工具配对用 `(role, tool_call_id)`（结构），不再"按段分队列"猜；
    文本就是消息对象的 `content`（真字符串），不再反转义 Python repr；
    说话的是谁按 `ev["role"]` 取，**不从 `tag` 的措辞里认**。
    """
    role = _role_of(ev)   # 配对键的一半 —— 两个 agent 的 id 空间会撞（见 `_TOOL_NAMES`）
    who = _ROLE_LABEL.get(role, "未知角色")
    for m in ev["messages"]:
        if len(_TOOL_NAMES) >= _TOOL_NAMES_CAP:
            _TOOL_NAMES.clear()   # 见上面的注释：配对只在同一轮内，清空是安全的
        for c in getattr(m, "tool_calls", None) or []:
            if c.get("id"):
                _TOOL_NAMES[(role, c["id"])] = str(c.get("name"))
            # ⚠️ **不截参数**：`delegate(task=...)` 就是交接本身，砍了等于看不到交接了什么。
            _emit("tool", f"{who} → {c.get('name')}（{trace.brief_args(c.get('args') or {})}）",
                  stamp=ev["ts"])
        kind = type(m).__name__
        if kind == "AIMessage":
            continue  # 它说的话已由流式实时播过（同旧契约：dump 不重复播 AI 正文）
        content = str(m.content)
        if kind == "ToolMessage":
            name = _TOOL_NAMES.get((role, getattr(m, "tool_call_id", "") or ""), "?")
            if name in _READ_TOOLS:
                # 🔴 **读全文的工具：一个字都不贴**（见 `_READ_TOOLS`）。
                _emit("ret", f"{name} 的返回：（**读了全文，{len(content)} 字** —— "
                             f"原文在{_READ_TOOLS[name]}里，**不在这里复述**）", stamp=ev["ts"])
            else:
                _emit("ret", f"{name} 的返回：\n{content}", stamp=ev["ts"])
        elif kind == "SystemMessage":
            # 🔴 **不截断**：平台注入是"优化 agent 表现"的观察窗口
            _emit("inject", f"平台 → 主 agent（每轮注入的任务台账）：\n{content}", stamp=ev["ts"])


def _on_event(ev: dict) -> None:
    """事件总线的订阅入口 —— 展示层从这里拿**结构化**事实，自己排版上屏。"""
    global _PENDING
    t, k = ev.get("type"), ev.get("kind") or ""
    # llm_io 的 PROMPT / RESPONSE / **「未逐字记录」那条 note** 归并进挂着的试答块
    # （同一次 LLM 调用 = 同一块）；其余任何事件到来都先关块 —— 时序不许倒。
    # ⚠️ `decision` 也**不关块**（2026-11-05）：它是这个块的**另一半**（结果），
    #    由下面那个分支接上去一起吐 —— 见 `k == "ask_main"` 那段的注释。
    # 🔴 **`note` 也必须算进来**（2026-11-06）：PROMPT 全文默认不逐字记 ⇒ 那次调用归并
    #    进来的是 note、不是 block。漏了它，块会**先被关掉**，然后 note 和四段**各自再吐
    #    一个 `🧭 DECIDE`** —— **一个块变三个**（实测：`unit_console` ⑬ 默认路那条当场红，
    #    报"吐了 3 次"）。`_SPECIAL_KINDS` 里 `llm_io` 的表态本来就是
    #    "那次 LLM 调用的 PROMPT/RESPONSE/**失败提示**"—— 三种载体都得走同一个块。
    _merges_into_block = t in ("block", "note") and k == "llm_io" and _PENDING is not None
    if not _merges_into_block and t != "decision":
        _close_pending()

    if t == "stream":
        # 谁说的 → 按 `role` 枚举取，**不从 `who` 的措辞里猜**（见 `_role_of`）
        _emit({trace.EXECUTOR: "say", trace.AGENT: "agent"}.get(_role_of(ev), "plat"),
              str(ev["text"]), stamp=ev["ts"])
    elif t == "dump":
        _on_dump(ev)
    elif t == "section":
        if k == "ask_main":
            # 🔴 **标题取事件里的 `title`，不许自己编**（2026-11-05 修）。
            #    原来这里写死了「【试答 · LLM 决策】平台强制的一步」—— 和业务侧那份
            #    （`agent._ASK_STEP`）**已经漂了**，而且用了 `【】`（段名专用符号）。
            #    后果：屏幕上**两个 `🧭 DECIDE` 块、两个标题**（PROMPT 一块、记录一块），
            #    读的人以为是**两次决策**。
            #    ⇒ 这一步的名字只有 `agent._ASK_STEP` 一份，业务侧把它放进
            #      `trace.section` 的标题（`safe_structured(tag=…)`）里带过来。
            #
            # 🔴 **而且这两半要合成一块**（用户 2026-11-05 的判据：*"所谓交接，就是
            #    外部给 LLM 什么东西、LLM 吐出什么东西"*）：**PROMPT 是这次决策的前提**
            #    （【前提】里写的就是"主 agent 的全部 N 条消息"—— PROMPT 正是那 N 条拼的），
            #    **四段是它的结果**。
            #    块在这儿开、PROMPT 由 `llm_io` 归并进来（**实时上屏，不黑屏**），
            #    四段在 `decision` 事件到达时**接到同一个块尾部**。
            # ⚠️ PROMPT 全文**默认不逐字记**（`PROTO_LOG_PROMPT`，见 `config.log_prompt_verbatim`）
            #    —— 那时归并进来的是**一条 note**（"未逐字记录（N 字）＋ 开关"），位置不变、
            #    块照样只有一个。**那条 note 必须在**（"切了要标"），见下面 `note` 分支。
            _PENDING = ["decide", [str(ev["title"])], ev["ts"]]
        elif k in _ROLE_BY_KIND:
            _emit(_ROLE_BY_KIND[k], str(ev["title"]), stamp=ev["ts"], cap=True)
    elif t == "block":
        if k == "llm_io" and _PENDING is not None:
            _PENDING[1].append(str(ev["label"]))
            _PENDING[1] += str(ev["text"]).splitlines() or [""]
        elif k == "executor_briefing":
            # 平台注入给执行器的**整份交接书**（任务 / 上次做到哪 / 历次答复 / 可读产物）
            _emit("inject", "平台 → 执行器：本次交接的全部已知\n" + str(ev["text"]),
                  stamp=ev["ts"])
    elif t == "decision":
        # 🔴 **接在挂着的块尾巴上**（2026-11-05）：那个块是**这次 LLM 调用的一端**
        #    （PROMPT，由 `ask_main` 开、`llm_io` 归并），四段是**另一端** ——
        #    分开吐的话，同一件事在屏幕上就是两个 `🧭 DECIDE`。
        #    ⚠️ 没有挂着的块（`log_prompt=False` 那类）就自己吐一块。
        if _PENDING is not None:
            _PENDING[1] += _render_decision(ev, with_who=False).splitlines()
            _close_pending()
        else:
            _emit("decide", _render_decision(ev), stamp=ev["ts"])
    elif t == "note":
        if k == "waiting":
            _emit("decide", "分支：主 agent 答不上 → " + str(ev["text"]), stamp=ev["ts"])
        elif k == "relay":
            # 🔴 **原样上屏**：`finalize: ` 那个前缀是**日志文件格式**，归记录层
            # （`recorder._NOTE_HEAD`）—— 这里曾经按**位置**切它（`[len("finalize: "):]`），
            # 文案措辞一改就静默吃掉 10 个字（2026-11-03 审核 F1 实测）。
            _emit("ask", "平台 → 用户：" + str(ev["text"])
                  + "\n（把上面那条**执行器的原话原样**转达 —— 代码保证不丢；主 agent 若也复述了一遍，"
                    "那是提示词层面的礼貌，不是平台重复）",
                  stamp=ev["ts"])
        elif k == "llm_io":
            # 🔴 **`llm_io` 的 note 必须看得见**：它是"这次调用的 PROMPT **没**逐字记"
            #    那条声明 —— **"切了要标"**（交接三问②），静默丢掉就是违反。
            #    落在**原来该有 PROMPT 的那个位置**（挂着的块里），所以块还是只有一个。
            #    ⚠️ 它原来**确实被静默丢了**：`note` 这条分支只认 `_ROLE_BY_KIND`，而
            #    `llm_io` 在 `_SPECIAL_KINDS` 里（那个集合只被 `block` 那条路用）；
            #    下面那道收尾自检也拦不住 —— 它三个集合都进了，算"表过态"。
            if _PENDING is not None:
                _PENDING[1] += [str(ev["text"])]
            else:
                _emit("decide", str(ev["text"]), stamp=ev["ts"])
        elif k in _ROLE_BY_KIND:
            _emit(_ROLE_BY_KIND[k], str(ev["text"]), stamp=ev["ts"], cap=True)
    elif t == "cut":
        # 🔴 **切了一刀就得看得见**（交接三问②）：标记落在**块的头**，不必长在切口上。
        _emit("plat", f"✂️ 截断 · {ev['where']}：保留 {ev['kept']} 字 / 原文 {ev['total']} 字"
                      + (f"｜{ev['see']}" if ev.get("see") else ""),
              stamp=ev["ts"], cap=True)
    # `handoff` 与未打 kind 的 note / section：只进日志文件，不上屏（同旧契约）。

    # 🔴 收尾自检：kind 既没归角色、也没有专门分支、又不在"只进日志"名单里 ——
    # 说明这一层**没对它表过态**，它就这么静默消失了（2026-11-03 审核 F3）。
    # 测试里 `unit_console` ⑪ 会先红；这是运行时那道网，防的是"测试没跑到"的路径。
    if t in ("note", "section", "block") and k and k not in _ROLE_BY_KIND \
            and k not in _SPECIAL_KINDS and k not in _LOG_ONLY_KINDS:
        if k not in _WARNED_KIND:
            _WARNED_KIND.add(k)
            _emit("plat", f"⚠️ 收到**没表过态**的 kind={k!r}（type={t}）—— 这一条没上屏。"
                          "去 `chat._ROLE_BY_KIND` / `_SPECIAL_KINDS` / `_LOG_ONLY_KINDS` 里表态。",
                  stamp=ev.get("ts"))


def _turn_summary(snap: dict, elapsed: float) -> None:
    """回合小结：**台账 + 主上下文读数**。

    人和主 agent 的话、工具调用、试答、投递……全都已在**实时播报**（事件订阅 `_on_event`）里
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


def _read_card_answer(color: str) -> str | None:
    """在待答卡片下读一条输入 —— **空行不作数**；跳过要**明说**（`skip` / `跳过`）。

    返回答复原文；`None` = 跳过（显式词 / Ctrl-C / EOF）。

    🔴 **为什么空行不再"跳过"**（2026-11-06，实测踩到）：`↩ 答它` 和 `👤 说别的`
    是两个提示符、**共享同一条 stdin** —— 一句话归谁，由"**哪个提示符先读到**"决定。
    实测一次真实会话：轮中缓冲里躺着一个杂换行
    （提前敲的回车 / 粘贴的尾随换行），`↩` 提示符一出现就被它"空行跳过"消费掉，
    用户随后打的**答复**落到 `👤`，变成了一条**新消息** —— 旧任务永远挂着
    （那次 `t8b06800d` 一直 ❓，卡片每轮重新摆）。
    ⇒ 空行是**杂音**，不许让它执行任何动作；跳过是一个**决定**，得打出来。
    （同 2026-10-30 主提示符那条"空行不再退出" —— 同一个教训，那次只改了主提示符。）
    """
    hinted = False
    while True:
        try:
            ans = input(paint("↩ 答它（打字即答复；要跳过就打 skip）┃ ", color)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if ans.lower() in _SKIP_WORDS:
            return None
        if ans:
            return ans
        if not hinted:
            print(paint("（空行不作数 —— 答复就直接打字；跳过请打 skip，回 👤 说别的）", color))
            hinted = True


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
    ap.add_argument(
        "--exec-path", choices=["auto", "delegate", "inline"], default="auto",
        help="执行路径：auto 两条都开（默认，产品行为）｜ delegate **强制委派**（主 agent 拿不到"
             "自己干的工具）｜ inline 强制主 agent 自己干（不给 delegate）。"
             "⚠️ 想验「执行器那条链路」时用它，别靠碰运气 —— 见 proto/agent.EXEC_PATH",
    )
    args = ap.parse_args()

    # 首次运行（只在打包后）：建 skills/ 和 runs/，空的话放一遍示例技能。
    # ⚠️ 返回的是"做了什么"（建目录 / 放技能），但**只有"放了技能"值得告诉用户** ——
    # "建目录"是内部事务，说了只是噪音。措辞也换成人话（不提目录名）。
    created = ensure_app_dirs()
    # ⚠️ 数字**由 `ensure_app_dirs` 报**，别写死 —— 原来硬编码"3 个"，
    # 而 `skills/` 里早就是 5 个包了（2026-11-02 空跑时发现：嘴上说 3、实际铺 5）。
    seed_note = next((c for c in created if "示例技能" in c), "")

    # 技能来自**文件夹**（不再绑 case）—— 用户 2026-10-30：*"有一个文件夹里面有各种 skill
    # cli 启动时候自动注入文件夹里面的全部 skill"*。case 那套是跑批用的（预置 turns + 断言），
    # 这个入口是给人自由对话用的。
    inst = install(
        {"skills_dir": args.skills, "skills_mode": "replace", "exec_path": args.exec_path}
    )
    graph = build()
    config = {"configurable": {"thread_id": args.thread}}
    session.use(args.thread)      # 🔴 进程就是一次会话（`thread_id` 进事件、日志带标识）

    from proto.config import describe

    label = Path(args.skills).name or "skills"
    tag = f"interactive-{label}-{time.strftime('%m%d-%H%M%S')}"
    # 记录落 `runtime.RUNS_DIR`：**exe 旁边 / 仓库顶层的 `runs/`** ——
    # 用户找得到，也方便整包发回来。
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
    artifacts_mod.use_dir(out_dir / "artifacts" / f"{tag}__r1")

    skills = registry.values()
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
    print(f"日志   {recorder.log_file()}")
    print(f"记录   {out_dir}")
    print(BAR)
    print("直接说你要什么。退出：Ctrl-C 或 exit。\n")
    if seed_note:
        # 只在打包后首次运行有（开发态目录本来就在）—— 用户得知道技能从哪来。
        print(f"  {seed_note} —— 你也可以自己往 skills 文件夹里加。\n")

    turns: list[dict] = []
    #: 哪几轮**没跑成**（平台故障）—— 单独记：`turns` 里只放跑成了的轮，
    #: 而"这一轮炸过"这件事得进记录（不然 results.json 看着像一切正常）。
    turn_errors: list[dict] = []
    seen_len = 0
    # 🔴 **展示层订阅事件总线**（2026-11-03 架构定）：`graph.invoke` 跑在主线程，
    # 业务代码每发一个事件，`_on_event` 就**同步**收到 —— 实时上屏，
    # 不再需要"另起线程 tail 日志文件 + 收尾 `_drain_once` 补漏"那整套机制。
    trace.subscribe(_on_event)
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
            # `graph.invoke` 是**阻塞**的（一轮 2–3 分钟），但过程**不会**黑屏：
            # 业务代码发的事件同步到达 `_on_event`，实时上屏（事件订阅，非 tail 日志）。
            #
            # 🔴 **炸一轮 ≠ 结束会话**（2026-11-04 复审 N2/R8）：原来整个 `while` 只有外面
            # 那一层 `except` —— 一次基础设施抖动（重试过仍失败）就把**整场会话**结束掉，
            # 用户白等一场、还得重新起进程。现在按**轮**兜：这一轮没跑成，说清原因，
            # 提示符接着来 —— 把刚才那句再说一遍就行。
            try:
                state = aio.run(graph.ainvoke(
                    {"messages": [HumanMessage(text)], "log": []}, config))   # 异步驱动
            except Exception as e:  # noqa: BLE001
                import traceback

                turn_errors.append({
                    "turn": len(turns) + 1, "input": text,
                    "error": f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}",
                })
                print(f"\n⚠️ **这一轮没跑成**（平台故障）：{type(e).__name__}: {e}")
                print("   会话还在 —— 把刚才那句**再说一遍**就行。\n")
                continue
            snap = snapshot(state, text, seen_len, len(turns) + 1, round(time.time() - t1, 1))
            snap["input_source"] = "human"  # 这句是用户当场打的，不是脚本预置
            turns.append(snap)
            seen_len = len(state.get("messages") or [])
            _turn_summary(snap, time.time() - t1)
            print()

            # 🔴 **待答：一次摆一个**（`solution.md` §0.65 Y1）—— 这个提示符**就是"通道"**：
            #    在它下面打的字**属于那一条请求**（带 `answer_to` 投回去，平台不猜"你在答谁"）；
            #    **直接回车 = 跳过** → 回到 `👤`，在那里打的字**只给主 agent**（这条待答继续挂着）。
            #    ⚠️ 摆哪一条由 `graph.next_pending` 定（**和投递认的是同一条规则**，一处定义）。
            while (p := next_pending(state)) is not None:
                icon, color = _STYLE["ask"]
                # ⚠️ **"取哪句、取不到怎么办"只有一处定义**（`asked_text`）——
                #    这里是**输入通道**那个视图，排版归自己，取法归它。
                q = asked_text(p)
                print(paint(f"{icon}【{p.get('skill')}】问：{q}", color))
                # 🔴 **空行不作数、跳过要明说** —— 见 `_read_card_answer` 的 docstring
                #    （2026-11-06：杂换行曾把用户的答复挤到 `👤` 那条通道去）。
                ans = _read_card_answer(color)
                if ans is None:  # 显式跳过（skip / 跳过）/ Ctrl-C / EOF → 回 👤
                    break
                t2 = time.time()
                try:
                    state = aio.run(graph.ainvoke(
                        {"messages": [HumanMessage(ans)], "log": [], "answer_to": p["id"]},
                        config,
                    ))
                except Exception as e:  # noqa: BLE001 —— 同主循环：炸一轮 ≠ 结束会话
                    print(f"\n⚠️ **这一轮没跑成**（平台故障）：{type(e).__name__}: {e}\n")
                    break
                snap = snapshot(state, ans, seen_len, len(turns) + 1, round(time.time() - t2, 1))
                snap["input_source"] = "human"
                snap["answer_to"] = p["id"]     # 记录层如实标：这句是在答哪一条
                turns.append(snap)
                seen_len = len(state.get("messages") or [])
                _turn_summary(snap, time.time() - t2)
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
        "turn_errors": turn_errors,
        "usage": inst["counter"].snapshot(),
        "artifact_writes": inst["writes"],
        "run_tasks": inst["run_tasks"],
        "artifact_ids": artifacts_mod.ids(),
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
            for k, v in artifacts_mod.items()
        },
        "fault": inst["fault"],
        "error": error,
        "wall_s": round(time.time() - t0, 1),
        "trace_log": str(recorder.log_file()),
        "skills_dir": str(args.skills),
    }
    dump_artifacts(out_dir, tag, 1, result)
    dump_run_record(out_dir, tag, 1, result)
    # 把这次跑的**原始 trace** 作为附录并进 `_run.md`（跑批那条路径是 run.py 干的，交互式得自己做）
    from app.runtime import append_trace

    append_trace(result, tag)
    if _SINK is not None:
        _SINK.close()
    (out_dir / "results.json").write_text(
        json.dumps([result], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    recorder.close()
    # 收尾**也只有一套**（用户 2026-11-02：*"结尾也统一起来 这个以打包为主"*）——
    # 留打包那版：**说清记录在哪 + 请对方发回来**。收尾才说路径不唐突
    # （刚做完事，这时候说"把它发我"才自然）；开场就说才叫噪音。
    # **回收是功能，不是附注。**
    # 🔴 **退出必须说一声**（2026-10-30）：原来一声不吭就结束，
    # 用户看到的就是"进程突然没了" —— 分不清是正常结束还是崩了。
    print(f"\n  再见 —— 这次对话已记录，请把这个文件夹发给我：\n    {out_dir}\n")


if __name__ == "__main__":
    main()
