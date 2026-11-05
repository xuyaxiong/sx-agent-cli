"""单元级：**交接单的"形式"** —— 每一跳都必须是同一套（不调 LLM）。

    uv run python -m evals.unit_handoff_form

**为什么要有它**（用户 2026-11-05）：

> *"这些统一都要有个统一的处理，不是随便想写啥就写啥 —— 比如决策单长什么样、
> 委托单长什么样，都应该从**形式上**确定下来。"*

收敛之前，6 跳里只有 2 跳（委托书 / 回执）走了共用机器，另外 4 跳**各拼各的字符串**：

| 违反 | 长什么样 |
|---|---|
| 段名后面直接跟内容 | 台账的 `【平台提示】你已委派出去的工作…` |
| 段名不在词汇表里 | 台账的 `【平台提示】`、透传的 `【lesson-prep】` |
| 续行缩进不是 2 的倍数 | 台账的 **6 格** |
| 根本没有段名 | 试答整张（`HumanMessage:` 这种框架类型名直接漏给模型） |

**形式（四条，`proto/handoff/__init__.py` 里定的）**：

    段名 0 列（独占一行）· 来源 2 列 · 段体 2 列 · 每层 +2

⚠️ **这条守卫管的是"形式"，不是"内容"** —— 内容对不对由 `unit_handoff` /
`unit_handoff_matrix` 那两条管。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import ast
import pathlib
import sys


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail else ""))


def _fixtures() -> dict[str, str]:
    """**每一跳渲染一遍** —— 键 = `HOPS` 里的跳名。"""
    from langchain_core.messages import AIMessage, HumanMessage

    from proto import artifacts
    from proto.artifacts import store_artifact
    from proto.executor import ExecOutcome
    from proto.graph import render_relay
    from proto.handoff.ask import build_prompt
    from proto.handoff import render_hop
    from proto.handoff.briefing import render_briefing
    from proto.handoff.ledger import render_ledger
    from proto.handoff.nudge import nudge_text
    from proto.handoff.receipt import build_receipt, render_receipt

    artifacts.clear()
    aid = store_artifact("《荷花》教案", "三年级下册完整教案", "md", "正文" * 60)
    artifacts.get(aid)["path"] = r"C:\runs\art_001_荷花-教案.md"
    q = "这节课你希望侧重哪种上法？（探究发现型 / 讲解示范型 / 练习巩固型 / 混合型）"
    task = {"id": "t1", "skill": "lesson-prep", "task": "生成《荷花》教案",
            "stage": "waiting_for_input", "artifacts": [], "refs": [aid],
            "asked": q, "report": "好的，第一个问题已发出。", "run_by": "delegate",
            "user_turns": [{"n": 1, "text": "帮我备《荷花》这一课", "after": ""}],
            "qa": [{"by": "用户", "q": q, "a": "探究发现型"}]}
    done = {**task, "stage": "completed", "artifacts": [aid], "asked": ""}
    wait = ExecOutcome(status="waiting_for_input", artifacts=[], rounds=1,
                       report="好的，第一个问题已发出。", asked=q)
    return {
        "briefing": render_briefing(task),
        "receipt": render_receipt(build_receipt(wait, note="平台已登记为待答")),
        "receipt_rejected": render_receipt(
            __import__("proto.handoff.receipt", fromlist=["build_rejection"])
            .build_rejection("rejected_skill", "❌ 没有技能「x」", "a、b")),
        "ledger": render_ledger([task, done]),
        "ask": build_prompt("lesson-prep", "生成《荷花》教案", wait,
                            [HumanMessage("帮我备《荷花》这一课"), AIMessage("好的，先派教案。")]),
        "nudge": nudge_text(),
        "relay": render_relay([task]),
        # ★ **第 7 跳**：任务文本（执行器打开就看的那一页）
        "task_text": render_hop("task_text", {
            "skill_instructions": "（技能指令全文……）",
            "known": render_briefing(task),
        }),
    }


def main() -> None:  # noqa: C901
    from proto.handoff import HOPS, SECTION

    rendered = _fixtures()

    print("① 每一跳都渲染得出来（`HOPS` 里声明的跳名，一个都不能漏）")
    for hop in HOPS:
        check(f"`{hop}` 渲染得出来", bool((rendered.get(hop) or "").strip()))

    print("\n② 🔴 **段名只能来自 `SECTION`**（`【…】` 是段名的专用符号）")
    # ⚠️ 这条抓的是**就地写的字面量**：台账原来的 `【平台提示】`、透传原来的
    #    `【lesson-prep】` —— 两个都在用段名的括号，却不在段名表里。
    for hop, text in rendered.items():
        stray = [ln for ln in text.splitlines()
                 if ln.startswith("【") and ln.strip() not in SECTION.values()]
        check(f"`{hop}`：凡以 `【` 开头的行，段名都在 `SECTION` 里", not stray,
              f"越界的行：{stray[:2]}")

    print("\n③ 🔴 **段名独占一行**（后面不跟内容）")
    for hop, text in rendered.items():
        stray = [ln for ln in text.splitlines()
                 if ln.startswith("【") and ln.strip() not in SECTION.values()]
        check(f"`{hop}`：段名独占一行", not stray, f"越界的行：{stray[:2]}")

    print("\n④ 🔴 **段体 2 列、每层 +2**（除段名行外，缩进必须是 ≥2 的偶数）")
    for hop, text in rendered.items():
        bad = [ln for ln in text.splitlines()
               if ln.strip() and not ln.startswith("【")
               and (len(ln) - len(ln.lstrip(" "))) % 2 != 0]
        loose = [ln for ln in text.splitlines()
                 if ln.strip() and not ln.startswith("【") and not ln.startswith("  ")]
        check(f"`{hop}`：段体缩进是偶数", not bad, f"越界的行：{bad[:2]}")
        check(f"`{hop}`：段体至少缩 2 格（没顶到 0 列）", not loose, f"越界的行：{loose[:2]}")

    print("\n⑤ 🔴 **渲染只有一条路** —— 段名行不许手拼（只能从 `SECTION` 取）")
    # ⚠️ **这条才是"形式定下来"的机制**：只要没人能手拼段名，形式就散不了。
    #
    # 🔴 **判据是"某一行以 `【` 开头"**，不是"字符串里出现 `【`" ——
    #    后者会把**引用**也当成手拼（"它说的话在上面【执行器说】里" 是合法的，
    #    那是**指路**，不是渲染）。实测踩过：判据放宽时报了 4 处假红，
    #    而真手拼的那几条（`f"【平台提示】你已委派出去的工作…"`、
    #    `f"【{who}】问：…"`）**恰好都以 `【` 开头**。
    root = pathlib.Path(__file__).resolve().parent.parent / "proto"
    hits: list[str] = []
    for py in root.rglob("*.py"):
        if py.name == "__init__.py" and py.parent.name == "handoff":
            continue  # ← 词汇表本身（`SECTION` 就住在这儿）
        src = py.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        skip = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                b = n.body
                if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) \
                   and isinstance(b[0].value.value, str):
                    skip.add(id(b[0].value))
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip:
                if any(ln.lstrip().startswith("【") for ln in n.value.splitlines()):
                    head = next(ln.strip() for ln in n.value.splitlines()
                                if ln.lstrip().startswith("【"))
                    hits.append(f"{py.relative_to(root.parent)}:{n.lineno} {head[:22]}")
    check("渲染器里没有手拼的段名行（`SECTION` 是唯一来源）", not hits,
          f"手拼的：{hits[:6]}")

    print("\n⑥ 🔴 **用词体系**（用户 2026-11-05）：同一个语义只许一个词")
    # ⚠️ **这条守的是"内容"那一半** —— ⑤ 守形式（段名不许手拼），这条守**用词**。
    #    用户原话：*"原则上交接单用词都应该是一个体系，这些词也是比较考究的，现在有些随意。"*
    #    ⚠️ 判据同 ⑤：**只看运行期可见的字符串**（排除 docstring）——
    #    注释里怎么说是**解释**，不是交给模型/用户看的话。
    #    ⚠️ **这些词"不许再用"是相对的**：`委派` 是定下来的那个，所以 `(?<!委)派` 才红。
    _BANNED: list[tuple[str, str, str]] = [
        (r"(?<!委)派(?!出|给)", "委派", "派发 / 派出去 / 派给 / 重派 / 派了"),
        ("成品", "产物", "成品"),
        ("请求信息", "要信息", "请求信息"),
        ("等答复", "在要信息", "等答复"),
        ("提问", "要信息 / 问", "提问"),
        ("一句话摘要", "梗概", "一句话摘要"),
    ]
    import re as _re
    bad: list[str] = []
    for py in root.rglob("*.py"):
        if py.name == "__init__.py" and py.parent.name == "handoff":
            continue  # ← 词汇表本身（它要**写出**那些"不许再用"的词，见 docstring）
        src = py.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        skip = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                b = n.body
                if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) \
                   and isinstance(b[0].value.value, str):
                    skip.add(id(b[0].value))
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip:
                for pat, good, why in _BANNED:
                    if _re.search(pat, n.value):
                        bad.append(f"{py.relative_to(root.parent)}:{n.lineno} 「{why}」→ 该用「{good}」")
    check("运行期字符串里没有「不许再用」的词", not bad, "\n      ".join(bad[:6]))

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败：")
        for f in FAIL:
            print(f"  ❌ {f}")
    print("═" * 78)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
