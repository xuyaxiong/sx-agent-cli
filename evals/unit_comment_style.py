"""单元级：**注释体例**（`docs/comment-style.md`，不调 LLM）。

    uv run python -m evals.unit_comment_style

**为什么要有它**（2026-11-06）：用户指着 `proto/handoff/__init__.py` 说
*"注释相对来说比较破碎，也没什么章法，对于人而言看起来是相当费劲的"*。
量下来"历史"只占 8% —— 病根是**导航 / 规则 / 沿革三类混在同一层**（47 个注释块，
18 处带日期，散布全篇）。体例定了五条，其中**两条能守卫**：

  ① 模块头必须有 `## 这个文件是什么`，且**标题只用那五个**（不许自创第六个）；
  ② **日期只许出现在「沿革」小节里**（`20\\d\\d-\\d\\d-\\d\\d`）；
  ③ **已退休的机制不许出现在「沿革」之外**（`RETIRED` 那张表）—— 2026-11-06 加的，
  第三轮外审 P2-2 的实锤：`executor.py` 的原则 2/3 与 `skills.py` 的两条"正解"，
  在讲一个**2026-11-05 就退休了的** LLM 调用。锚是对的（〔沿革 3〕〔沿革 13〕都写着
  "退休"），**正文没跟上** —— 这条守的就是"顺着锚回头改正文"。

⚠️ **另外三条守卫不了**（表项注释写得有没有章法、版式分不分得清、一条规矩有没有写两遍）——
那是给人读的。这里如实说，不假装有机制。
⚠️ ③ 守的也只是**有符号可抓**的那类（"判定提示词"这种短语）。**无符号的断言**
（"判定怎么问，很关键"）它抓不到 —— 除非那句话里恰好有表里的词。

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
os.environ["PROTO_LOG_DIR"] = "tmp/logs"   # 🔴 测试日志是**噪音** —— 别混进给人看的 `logs/`

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

#: 🔴 **已退休的机制** —— 这些词出现在**「沿革」之外**就是注释腐烂：正文在讲一个
#: **不存在的东西**（实锤见模块头 ③）。⚠️ 只在**「沿革」里**出现是对的（那里是历史）。
#:
#: ⚠️ **只许追加**（退休是新发生的）。**删一条 = 说"这东西又回来了"** —— 那就得同时
#: 改这条注释，并在评审里说清。
#: ⚠️ **只收"一出现就错"的词**：像 `completion_criteria` / `build_prompt` 这种
#: **现在仍被正当提及**的（前者是"平台故意不要求的字段"，后者是试答在用的函数）
#: **不许收进来** —— 收了就是假红，而假红会让人开始忽略这个守卫。
RETIRED: tuple[str, ...] = (
    "判定提示词",
    "判定怎么问",
    "判定所需的信息",
    "判定看不到",
    "判定用三态",
)

#: 一处证据引用（`group(1)` 是整段，含前缀目录）。
#: ⚠️ 尾巴**只收路径字符**（ASCII ＋ `…`）—— 收中文的话，"建 `runs/`，空的话放一遍示例
#: 技能"这种**裸目录名**会被连成一串误判（实测踩过）。
_EVIDENCE = re.compile(r"(?<![\w/])((?:logs|runs|tmp)/[A-Za-z0-9_./\-…]*)")


def evidence_refs(path: str) -> list[str]:
    """**证据引用**不许指向不入库的目录 —— 返回这个文件里的违规处。

    🔴 由来（第三轮外审 P2-3）：`logs/` `runs/` `tmp/` **三个都在 `.gitignore` 里**。
    实测：`logs/` 磁盘 **2146** 个文件、**版本库里 0 个**；`runs/` **整个已经没了**
    —— `evals/cases/t-feed-rerun.yaml` 原来引的 `runs/f5-rerun-verify/…__r2` 就是指向它，
    而那句注释还写着"✅ 实测到了"。
    ⇒ 留一个**具体路径**就是留一个**迟早会指空的指针**，而且**失效是静默的**：文件被清掉，
    注释照旧写着"实测到了"，读的人**不会发现证据没了**（判据②的同源：「切了不标是最糟的」）。
    ⇒ 正解：**证据要么跟注释一起进版本库（内联几行），要么留一条能重跑出它的命令。**

    ⚠️ **通配 / 占位不算**（`logs/proto-<时间戳>.log` 是在说**命名规则**，不是在指某个文件）；
    **裸目录名也不算**（"建 `skills/` 和 `runs/`" 是在说建目录）。
    ⚠️ **「沿革」不豁免** —— 沿革里引证据一样会指空（它是历史，不是免责）。
    """
    src = io.open(path, encoding="utf-8").read()
    out: list[str] = []
    for i, line in enumerate(src.splitlines(), 1):
        for m in _EVIDENCE.finditer(line):
            tail = m.group(1).split("/", 1)[1].rstrip("/")
            # ⚠️ **紧跟占位符 = 在说命名规则，不是在指文件**：`logs/proto-<时间戳>.log`
            #    的尾巴止于 `proto-`，而下一个字符就是 `<`。这一条不能少 —— 少了它，
            #    全仓那些通配引用会全部假红。
            nxt = line[m.end():m.end() + 1]
            if not tail or any(c in tail for c in ("*", "<", "{", "$")) \
                    or nxt in ("*", "<", "{", "…"):
                continue
            # ⚠️ **裸目录名不是证据引用**：`tmp/logs` 这种**没扩展名、也没下一层**的，
            #    是在说"放哪儿"，不是在指某个文件（实测踩过：`PROTO_LOG_DIR = "tmp/logs"`
            #    被当成引用，**假红**）。**有 `.`（扩展名）或有 `/`（下一层）才算。**
            if "." not in tail and "/" not in tail:
                continue
            out.append(f"{path}:{i}  {m.group(1)}")
    return out

#: 🔴 **欠账清单**：还没按体例整理的模块 —— **只许缩短**（改完一个删一行）。
#: ✅ **2026-11-06：清单空了。** 全仓 `proto/` 的 19 个模块都按体例整理过一遍
#: （每个都用 AST 比对证明过"代码一字未动"；过程中翻出 ~15 处"注释与代码对不上"，
#: 逐条核实后修掉）。
#: ⚠️ 以后**加进来要在评审里说清**（它是"欠账"，不是"豁免"）。
PENDING: set[str] = set()

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
    # ③ 已退休的机制不许出现在「沿革」之外 —— 正文在讲一个**不存在的东西**。
    #    有效性自检：把 `executor.py` 原则 2 改回"**判定怎么问**，很关键" → 这里必须红。
    rot = [f"{i + 1}:{lines[i].strip()[:60]}"
           for i in range(len(lines))
           if i not in hist_range and any(w in lines[i] for w in RETIRED)]
    if rot:
        out.append("「沿革」之外还在讲**已退休的机制**（那东西已经不在了）：\n        "
                   + "\n        ".join(rot[:6]))
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

    print("\n④ 证据引用不许指向**不入库**的目录（`logs/` `runs/` `tmp/`）")
    # 🔴 第三轮外审 P2-3。**有效性自检**：往任意一个 `proto/` 或 `evals/` 文件里写一句
    #    「实测（`logs/x.log`）」→ 这里必须红。
    _scan = sorted(
        glob.glob("proto/**/*.py", recursive=True)
        + glob.glob("evals/**/*.py", recursive=True)
        + glob.glob("evals/**/*.yaml", recursive=True)
        + glob.glob("docs/**/*.md", recursive=True)
    )
    # ⚠️ **本文件自己豁免**：它必须**写出**那些词才能说明这条规矩（同
    #    `handoff/__init__.py` 对"段名不许手拼"那条守卫的豁免）。
    _self = os.path.abspath(__file__).replace(os.sep, "/")
    ev = [h for f in _scan
          if os.path.abspath(f).replace(os.sep, "/") != _self
          for h in evidence_refs(f.replace(os.sep, "/"))]
    check("没有注释在引用 `logs/` `runs/` `tmp/` 里的**具体文件**"
          "（证据要么内联、要么给一条能重跑的命令）", not ev,
          "\n      ".join(ev[:8]))

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
