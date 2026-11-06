"""单元级：**注释体例**（`docs/comment-style.md`，不调 LLM）。

    uv run python -m evals.unit_comment_style

**为什么要有它**（2026-11-06）：用户指着 `proto/handoff/__init__.py` 说
*"注释相对来说比较破碎，也没什么章法，对于人而言看起来是相当费劲的"*。
量下来"历史"只占 8% —— 病根是**导航 / 规则 / 沿革三类混在同一层**（47 个注释块，
18 处带日期，散布全篇）。体例定了五条，其中**两条能守卫**：

  ① 模块头必须有 `## 这个文件是什么`，且**标题只用那五个**（不许自创第六个）；
  ② **日期只许出现在「沿革」小节里**（`20\\d\\d-\\d\\d-\\d\\d`）。

⚠️ **另外三条守卫不了**（表项注释写得有没有章法、版式分不分得清、一条规矩有没有写两遍）——
那是给人读的。这里如实说，不假装有机制。

`PENDING` = **欠账清单**（还没按体例整理的模块）：**只许缩短**。
- 不在清单里的文件**必须**合格；
- 在清单里的文件**必须仍然不合格**（改好了就该删掉那一行 —— "别留着烂"）。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import os

# 🔴 **单测的日志打 tag**（2026-11-06）：`logs/` 里真会话与测试垃圾混在一起，复盘时
#    grep 不动 —— 打上 tag 后文件名是 `proto-<ts>-unit-<名字>.log`，一眼可辨
#    （清理：`rm logs/*-unit-*.log`）。复用跑批那个 `PROTO_LOG_TAG`（`evals/_child.py`）。
# ⚠️ **必须排在所有非标准库 import 之前**：`proto.trace` 在 import 那一刻读 env 定文件名，
#    而 `from evals import chat` 这种**间接** import 也会把它拉进来（实测踩过：
#    第一版插在 `from evals import chat` 之后 → 日志照样没 tag）。
#    守卫：`unit_feed_waiting` ⑮。
os.environ["PROTO_LOG_TAG"] = "unit-comment-style"

import ast
import glob
import io
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS: list[str] = []
FAIL: list[str] = []

#: 允许的小节标题（**别自创第六个** —— 见 `docs/comment-style.md`）
HEADINGS = {
    "这个文件是什么",
    "设计原则（不变量）",
    "关键取舍",
    "遗留问题",
    "沿革（改过什么、为什么改）",
}
#: 必须有那一个
REQUIRED = "这个文件是什么"
#: 「沿革」小节的标题（日期只许落在它里面）
HISTORY = "沿革（改过什么、为什么改）"
#: 一行的日期（体例第 5 条）
DATE = re.compile(r"20\d\d-\d\d-\d\d")

#: 🔴 **欠账清单**：还没按体例整理的模块 —— **只许缩短**（改完一个删一行）。
#: ⚠️ 加进来要在评审里说清（它是"欠账"，不是"豁免"）。
PENDING = {
    "proto/agent.py",
    "proto/artifacts.py",
    "proto/config.py",
    "proto/executor.py",
    "proto/graph.py",
    "proto/llm.py",
    "proto/registry.py",
    "proto/skill_pack.py",
    "proto/skills.py",
    "proto/trace.py",
    "proto/__main__.py",
    "proto/handoff/ask.py",
    "proto/handoff/briefing.py",
    "proto/handoff/decision.py",
    "proto/handoff/ledger.py",
    "proto/handoff/nudge.py",
    "proto/handoff/receipt.py",
}

#: 一行 docstring 的包标记 —— 没有正文可分段（不是欠账）
EXEMPT = {"proto/__init__.py"}


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail else ""))


def _module_doc(tree: ast.Module) -> ast.Expr | None:
    if tree.body and isinstance(tree.body[0], ast.Expr):
        v = tree.body[0].value
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            return tree.body[0]
    return None


def violations(path: str) -> list[str]:
    """返回这个文件违反体例的地方（空 = 合格）。"""
    src = io.open(path, encoding="utf-8").read()
    lines = src.splitlines()
    tree = ast.parse(src)
    out: list[str] = []
    doc = _module_doc(tree)
    if doc is None:
        return ["没有模块 docstring（体例第 1 条：模块头要说清自己是什么）"]
    d_start, d_end = doc.lineno - 1, doc.end_lineno - 1
    doc_lines = lines[d_start:d_end + 1]
    heads = [m.group(1) for m in (re.match(r"^##\s+(.+?)\s*$", l) for l in doc_lines) if m]
    # ① 标题
    if REQUIRED not in heads:
        out.append(f"模块 docstring 里没有 `## {REQUIRED}`")
    extra = [h for h in heads if h not in HEADINGS]
    if extra:
        out.append(f"自创的小节标题：{extra}（只许用 {sorted(HEADINGS)}）")
    # ② 日期只许在「沿革」
    hist_at = next((i for i, l in enumerate(doc_lines)
                    if l.strip() == f"## {HISTORY}"), None)
    hist_range = range(d_start + hist_at, d_end + 1) if hist_at is not None else range(0)
    stray = [f"{i + 1}:{lines[i].strip()[:60]}"
             for i in range(len(lines))
             if DATE.search(lines[i]) and i not in hist_range]
    if stray:
        out.append("日期出现在「沿革」之外：\n        " + "\n        ".join(stray[:6]))
    return out


def main() -> None:
    files = sorted(glob.glob("proto/*.py") + glob.glob("proto/handoff/*.py"))
    files = [f.replace(os.sep, "/") for f in files]

    print("① 不在欠账清单里的模块：**必须**合格")
    bad: dict[str, list[str]] = {}
    for f in files:
        if f in PENDING or f in EXEMPT:
            continue
        v = violations(f)
        if v:
            bad[f] = v
    check("清单外的模块都按体例整理过（日期只在「沿革」＋ 模块头有「这个文件是什么」）",
          not bad,
          "\n      ".join(f"{f}: {v[0]}" for f, v in bad.items()))

    print("\n② 清单里的模块：**必须仍然**不合格（改好了就删掉那一行 —— 别留着烂）")
    stale = [f for f in sorted(PENDING) if not violations(f)]
    check("欠账清单里没有「已经改好、却还挂着」的", not stale, repr(stale))

    print(f"\n③ 欠账清单还剩 **{len(PENDING)}** 个模块")
    for f in sorted(PENDING):
        n = len(violations(f))
        print(f"      · {f}（{n} 处不合体例）")
    print("      ⚠️ 只许缩短：改完一个就从 `PENDING` 里删一行。")

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
