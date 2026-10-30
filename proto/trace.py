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


def brief_args(args: Any, limit: int = 120) -> str:
    """工具参数的一行摘要 —— 短值原样，长值只留字数（正文类参数不往日志里灌）。"""
    if not isinstance(args, dict):
        return str(args)[:limit]
    parts = []
    for k, v in args.items():
        text = str(v)
        parts.append(f"{k}={text!r}" if len(text) <= limit else f"{k}=<{len(text)}字>")
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


def render_message(m) -> str:
    """把一条 LangChain 消息渲染成可读文本。"""
    kind = type(m).__name__
    content = str(m.content)
    parts = [f"{kind}({len(content)}字)"]
    if content.strip():
        # AI 的话给宽一点（**日志可读性**：执行器的提问往往在正文末尾，截 400 就看不到了）；
        # 工具返回仍压到 400（那里面多是技能原文/产物正文，另有出处）。
        cap = 2000 if kind == "AIMessage" else 400
        parts.append(f"    content={content[:cap]!r}" + ("…" if len(content) > cap else ""))
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
