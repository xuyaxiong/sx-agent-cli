"""把原型跑的每一步写进文件，方便事后翻看「我问了 LLM 什么、它答了什么」。

日志落在**应用根**下的 `logs/proto-<时间戳>.log`（开发时是 `langgraph-lab/logs/`，
打包后是 exe 旁边的 `logs/`），同时打到控制台（简洁版）。
每次进程启动开一个新文件，不覆盖历史。
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from proto.config import app_root

#: 日志落**应用根**下的 `logs/`（见 `proto.config.app_root`）——
#: 开发时是 `langgraph-lab/logs/`，打包后是 **exe 旁边的 `logs/`**。
#: ⚠️ 不能按 `__file__` 算：冻结后那是 `_internal/`，用户不该碰、重建还会被清掉。
LOG_DIR = app_root() / "logs"
LOG_DIR.mkdir(exist_ok=True)

#: 文件名后缀 —— 时间戳只到**秒**，跑批时多个子进程同秒启动会**撞同一个文件**。
#: `evals/` 的子进程用 `PROTO_LOG_TAG=<case>-r<rep>` 区分；手工跑时不设，行为不变。
_TAG = os.getenv("PROTO_LOG_TAG", "").strip()
_SUFFIX = f"-{_TAG}" if _TAG else ""

LOG_FILE = LOG_DIR / f"proto-{time.strftime('%Y%m%d-%H%M%S')}{_SUFFIX}.log"
_fh = LOG_FILE.open("w", encoding="utf-8")

BAR = "═" * 78


def stamp() -> str:
    """日志时间戳：**年月日 + 毫秒**（用户 2026-10-30：*"日志实践精确到毫秒 添加上年月日信息"*）。

    以前只有 `HH:MM:SS` —— 一轮里同一秒内发生好几件事（工具调用 → 返回 → 判定），
    秒级看不出先后；跨天的日志也分不清是哪天。
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


#: 内部沿用的短名（历史调用点都写 `_stamp()`）
_stamp = stamp


def write(line: str = "") -> None:
    """写一行到日志文件（不自动换行缩进，原样写）。"""
    _fh.write(line + "\n")
    _fh.flush()


def section(title: str) -> None:
    write()
    write(f"{BAR}\n[{_stamp()}] {title}\n{BAR}")


def note(text: str) -> None:
    write(f"[{_stamp()}] {text}")


def block(label: str, text: str, *, indent: str = "  ") -> None:
    """写一个带标签的多行文本块（prompt / response 全文）。"""
    write(f"[{_stamp()}] {label}:")
    for line in str(text).splitlines() or [""]:
        write(f"{indent}{line}")


def kv(label: str, **fields: Any) -> None:
    """写一行键值对。"""
    inner = " | ".join(f"{k}={v}" for k, v in fields.items())
    write(f"[{_stamp()}] {label} | {inner}")


#: **正文类参数**：只留字数。
#:
#: 🔴 **只有它们该省**（2026-10-30 用户纠正）：`write_artifact(content=...)` 的正文
#: **另有出处**（产物文件），灌进日志纯属浪费。
#:
#: ⚠️ **其余参数一律照原样给全** —— 原来按"超过 120 字就省"一刀切，
#: 结果把 `delegate(task=...)` 也省成了 `<220字>`，而**那正是整个交接的核心**
#: （用户：*"你怎么把这么关键的上下文交接信息给去掉了？？"*）。
#: 判据跟显示层那条一样：**全文另有出处才省，没有就全给**。
BODY_ARGS = frozenset({"content"})


def brief_args(args: Any, limit: int = 120) -> str:
    """工具参数的一行摘要 —— **只有 `BODY_ARGS` 里的正文类参数**会被压成字数。"""
    if not isinstance(args, dict):
        return str(args)
    parts = []
    for k, v in args.items():
        text = str(v)
        parts.append(f"{k}=<{len(text)}字>" if k in BODY_ARGS and len(text) > limit else f"{k}={text!r}")
    return ", ".join(parts)


def tool_calls_of(messages: list) -> list[tuple[str, str]]:
    """从消息里抽出**全部**工具调用 —— `(名字, 参数摘要)`，按发生顺序。

    主 agent 的 `read_artifact` 这类调用，原来在日志里查不到（只有执行器的轨迹被 dump），
    只能靠"消息类型对不上"反推。这个函数是给日志补上那一段的。
    """
    out: list[tuple[str, str]] = []
    for m in messages:
        for c in getattr(m, "tool_calls", None) or []:
            out.append((str(c.get("name")), brief_args(c.get("args") or {})))
    return out


#: 流式打出来的正文行长这样：`[时间戳] ⟨生成·主 agent⟩ 这一行的原文`
#: （`chat._feed` 认这个前缀，实时推到终端）。
STREAM_MARK = "⟨生成·"


def stream_agent(graph, state, config, who: str):
    """跑一个 agent 图，**每一步 + 每个字**都录下来 —— 边跑边写日志。

    Yields：每个 super-step 的完整 state（`stream_mode="values"` 那一支）。

    🔴 **为什么要有它**（2026-10-30）：`invoke` 会把整个 agent 循环跑完才返回，
    于是"模型→工具→模型→工具…"这一串**全程静默**。实测一次真实会话：
    主 agent 连调 8 个工具、**336 秒里日志一行不动** —— 而 CLI 是 tail 日志的，
    用户侧就是**黑屏**，分不清"在干活"还是"卡住了"。

    两层一起给：
      - **每个 super-step** → `values` 那一支，调用方拿去做逐步骤 dump
      - **每个字** → `messages` 那一支，**攒到换行就写一行日志**
        （日志是按行读的，逐字写进去 CLI 也看不见）

    ⚠️ **前提：所有 `AIMessage` 都从这里流过** —— 所以 `render_message` 对 AI 的话
    **只写头部、不再重复正文**（正文已经在上面流出来了）。改调用点时记得这条。

    ⚠️ 多个执行器**并行**时，它们的流式行会交错 —— 所以 `who` 要能分辨是谁
    （主 agent / 执行器·`<技能名>`）。

    ⚠️ **只流 `node='model'` 的 chunk** —— 工具内部的结构化调用（判定 / 试答）
    流出来是裸 JSON，既不可读、又和已解开的展示重复，见下面的注释。
    """
    buf = ""
    cur_id: str | None = None      # 当前这条 AI 消息的 id —— 换了消息就把 buf 落盘

    def flush_line(line: str) -> None:
        if line.strip():
            write(f"[{_stamp()}] {STREAM_MARK}{who}⟩ {line}")

    for mode, payload in graph.stream(state, config, stream_mode=["values", "messages"]):
        if mode == "messages":
            chunk, meta = payload
            # 🔴 **只流 agent 自己说的话**（`node='model'`）。
            #
            # `node='tools'` 是**工具内部的 LLM 调用** —— 实测就是判定 / 试答那几个
            # **结构化输出**（它们跑在 `delegate` 里面，却被外层的 stream 捕获）。
            # 流出来的是**裸 JSON**：既不可读（用户 2026-10-30：*"就打个 json 算啥"*），
            # 又和**已经解开的展示重复**（`轮判定：status=… 理由：… 进度：… 还缺：…`）。
            # 而且连续两次结构化调用会黏成一行（`}{`）。
            #
            # 所以这里**跳过**：它们的**结论**在 `_feed` 的「判定 / 试答」分支里
            # 已经是人话，这里再流一遍只是噪音。
            if meta.get("langgraph_node") != "model":
                continue
            # 🔴 **换了消息就先把缓冲区落盘**（2026-11-02 实测踩到）。
            #
            # 模型调工具前会先说一句、工具跑完再接着说，**两段之间没有换行符** ——
            # 攒在同一个 `buf` 里就被黏成一句。实测现象（`interactive-skills-1102-174302`）：
            #
            #   好的，信息齐了，我来生成导学单已生成，以下是交付情况：
            #   └─ 调 delegate 前说的 ──────┘└─ 工具返回后说的 ──┘
            #
            # 中间隔着 **85 秒**（执行器在干活），日志上却是连着的一句话。
            # 判据用 `chunk.id`：同一条 AI 消息的流式 chunk 共享 id，换一次模型调用就换 id。
            # ⚠️ 有些 provider 不给 id（`None`）—— 那时**保持原行为**，不要瞎猜边界。
            cid = getattr(chunk, "id", None)
            if cid and cur_id and cid != cur_id:
                flush_line(buf)
                buf = ""
            if cid:
                cur_id = cid

            text = _chunk_text(chunk)
            if not text:
                continue
            buf += text
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                flush_line(line)
            continue
        yield payload
    flush_line(buf)  # 收尾：最后一行没有换行符



def _chunk_text(chunk) -> str:
    """从一个流式 chunk 里取纯文本（内容可能是 str，也可能是分块列表）。"""
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(p.get("text") or "") if isinstance(p, dict) else str(p) for p in content
        )
    return ""


def render_message(m) -> str:
    """把一条 LangChain 消息渲染成可读文本。

    🔴 **这里不截断**（2026-10-30 用户定）：日志是**原始记录**，
    而"平台注入了什么、agent 说了什么"正是**优化 agent 表现的唯一观察窗口** ——
    *"这是优化agent表现的细节 如果没有这些东西观察不到"*。
    要少看噪音就在**播报层**（`chat._feed`）砍，别在记录层砍。

    ⚠️ **AI 的话只写头部，不重复正文** —— 正文已经由 `stream_agent` **逐行流过**了
    （见它的 docstring）。再写一遍的话：日志里两份、终端里也两份。
    """
    kind = type(m).__name__
    content = str(m.content)
    parts = [f"{kind}({len(content)}字)"]
    # 🔴 **平台注入的标记**（`proto.handoff.INJECTED`）—— 透传注入的是 AIMessage，
    # 内容是**执行器的原话**；不写出来，读日志的人会以为那是主 agent 说的。
    tag = (getattr(m, "additional_kwargs", None) or {}).get("injected_by")
    if tag:
        parts.append(f"    injected_by={tag}")
    if content.strip() and kind != "AIMessage":
        # `!r` 只为**压成一行**（日志一行一条），不是为了截断。
        parts.append(f"    content={content!r}")
    for c in getattr(m, "tool_calls", None) or []:
        parts.append(f"    tool_call={c.get('name')} args={brief_args(c.get('args') or {})}")
    return "\n".join(parts)


def dump_trace(tag: str, messages: list) -> None:
    """把一整条消息轨迹写下来（执行器内部发生了什么，全在这）。"""
    write(f"[{_stamp()}] {tag} 消息轨迹（{len(messages)} 条）:")
    for i, m in enumerate(messages):
        write(f"  [{i}] {render_message(m)}")


def close() -> None:
    _fh.close()


# ──────────────────────────── 交接：关键日志（便于复盘） ────────────────────────────


def handoff(
    hop: str,
    frm: str,
    to: str,
    text: str,
    *,
    structured: str = "",
    full: bool = False,
) -> None:
    """**一次交接的完整记录** —— 一行摘要（＋可选全文）。

    用户 2026-11-03：*"你得加上关键日志 —— 这些在日志中得完整记录下来**便于复盘**。"*

    摘要行让复盘的人**一眼看到**：
      - **谁给谁、交了多少字** → 判据①（内容）
      - **有没有结构化对象** → 判据④（结构 ＋ 模板两层分离）
      - **跳名与方向** → 判据⑤（这一跳是平台硬拼还是 LLM 转述）

    截断（判据②）由**切的那一方**自己报（`cut()`）—— 不在这里事后解析文本。
    膨胀（判据③）看每轮的上下文读数（`harness.snapshot` / CLI 的 `📊 CONTEXT` 行）。

    ⚠️ `full=True` 只在**别处没有 dump 过**这一跳时用（否则日志里两份）。
    """
    note(
        f"📤 交接 · {hop}（{frm} → {to}）：{len(text)} 字"
        f"｜结构化：{structured or '（纯文本）'}"
    )
    if full:
        block(f"交接全文 · {hop}", text)


def cut(where: str, kept: int, total: int, *, see: str = "") -> None:
    """**切了一刀就报一刀** —— 由切的那一方自己调，不靠事后从文本里解析。

    用户 2026-11-03：*"主动截断是否有显式标记，被截断的内容，有没有告知它能查到
    全文详情的方式方法。"* —— 标记在**文本里**（读的人看得见），
    这一行在**日志里**（复盘的人看得见），两者都要有。

    `see` = 全文在哪（渐进式披露的出口）。
    """
    note(f"✂️ 截断 · {where}：保留 {kept} 字 / 原文 {total} 字" + (f"｜全文：{see}" if see else ""))
