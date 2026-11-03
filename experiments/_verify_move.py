"""一次性校验：交接搬进 `proto/handoff/` 后，渲染**逐字没变**。

    uv run python -m experiments._verify_move

做法：把 **HEAD 里的旧函数**取出来（`git show HEAD:<file>` + `ast` 抽函数），
和新代码喂**同样的输入**，逐字比 —— 不靠"我觉得应该一样"。

⚠️ **只在"搬家进行中"有用**：它比的是 HEAD 与工作区。搬完一提交，
HEAD 就成了新代码，比对退化成"自己跟自己比"。
留着是**为了下次搬家**（同样的手法，改一下路径和函数名就能用）。

⚠️ **它验不到模块级依赖**（2026-11-03 实测踩到，见 `discussion-log` F24）：
纯函数比对全绿，而抽函数时漏带的 import 一路绿到真跑才炸。
**所以搬家还要跑 `evals/unit_handoff.py` ⑫（挨个渲染器调一遍）。**
"""

# ruff: noqa: T201  # 诊断输出即本产出物

from __future__ import annotations

import ast
import subprocess
import sys
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail and not ok else ""))


def head_funcs(path: str, names: tuple[str, ...], extra_ns: dict) -> dict:
    """从 HEAD 版本的文件里抽出这几个函数，在一个干净命名空间里 exec。"""
    src = subprocess.run(["git", "show", f"HEAD:{path}"], capture_output=True,
                         text=True, encoding="utf-8").stdout
    ns = {"Any": Any, **extra_ns}
    nodes = []
    for n in ast.parse(src).body:
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names:
            nodes.append(n)
        # 模块级常量（`_STAGE_MARK` 这种）也要 —— 它们是 `ast.Assign`，不是 def
        elif isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in names for t in n.targets
        ):
            nodes.append(n)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), f"<old:{path}>", "exec"), ns)
    return ns


def main() -> None:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    # ── ① 台账：HEAD 的 `node_prepare` vs 新的 `render_ledger` ──
    print("① 台账（`graph.node_prepare` → `handoff/ledger.render_ledger`）")
    from proto.handoff.ledger import render_ledger

    old = head_funcs(
        "proto/graph.py",
        ("_STAGE_MARK", "_NEEDS_DETAIL", "_brief", "node_prepare"),
        {"SystemMessage": SystemMessage, "_feed_waiting": lambda state: ([], [])},
    )
    tasks = [
        {"id": "t1", "skill": "lesson-prep", "stage": "waiting_for_input", "artifacts": [],
         "progress": "已做完第一步", "missing": ["课时", "学情"], "report": "请问课时是？" * 20},
        {"id": "t2", "skill": "quiz", "stage": "completed", "artifacts": ["art_001"]},
        {"id": "t3", "skill": "guided-learning", "stage": "failed", "artifacts": [],
         "progress": "", "missing": [], "report": "干不动"},
        {"id": "t4", "skill": "x", "stage": "superseded", "artifacts": []},
    ]
    a = old["node_prepare"]({"tasks": tasks})["messages"][0].content
    b = render_ledger(tasks)
    check("台账逐字相同（含截断标记 / 两种 hint）", a == b, f"旧 {len(a)} / 新 {len(b)}")
    if a != b:
        for x, y in zip(a.splitlines(), b.splitlines()):
            if x != y:
                print("      首个不同:", repr(x[:70]), "≠", repr(y[:70]))
                break
    # 没有待答时走另一条 hint
    a2 = old["node_prepare"]({"tasks": [tasks[1]]})["messages"][0].content
    check("台账逐字相同（无待答那条 hint）", a2 == render_ledger([tasks[1]]))

    # ── ② 推回去：HEAD 的 `_nudge_text` vs 新的 `nudge_text` ──
    print("\n② 推回去（`executor._nudge_text` → `handoff/nudge.nudge_text`）")
    from proto.handoff.judgement import _Judgement
    from proto.handoff.nudge import nudge_text

    old = head_funcs("proto/executor.py", ("_nudge_text",), {})
    for label, j in (
        ("有 progress/missing", _Judgement(status="incomplete", progress="做了一半", missing=["a", "b"])),
        ("全空（判定调用失败那条）", _Judgement(status="incomplete")),
    ):
        check(f"nudge 逐字相同（{label}）", old["_nudge_text"](j) == nudge_text(j))

    # ── ③ 判定提示词：HEAD 的 `_judge` 拼的 prompt vs 新的 `build_prompt` ──
    print("\n③ 判定（`executor._judge` 里拼的 prompt → `handoff/judgement.build_prompt`）")
    from proto.handoff.judgement import build_prompt

    captured: dict = {}
    old = head_funcs(
        "proto/executor.py",
        ("_judge", "_JUDGE_RULES"),
        {
            "_Judgement": _Judgement,
            "ARTIFACTS": {},
            "trace": type("T", (), {"note": staticmethod(lambda *a, **k: None)}),
            "safe_structured": lambda schema, prompt, default, **kw: captured.update(prompt=prompt) or default,
        },
    )
    skill = type("S", (), {"name": "quiz", "instructions": "产出 3 道小题。你需要知道难度。"})()
    old["_judge"](skill, [], "材料正文", "已知信息正文")
    new_prompt = build_prompt(skill.instructions, [], "材料正文", "已知信息正文")
    check("判定 prompt 逐字相同", captured.get("prompt") == new_prompt,
          f"旧 {len(captured.get('prompt') or '')} / 新 {len(new_prompt)}")

    # ── ④ 试答：HEAD 的 `_ask_prompt` vs 新的 `handoff/ask.build_prompt` ──
    print("\n④ 试答（`agent._ask_prompt` → `handoff/ask.build_prompt`）")
    from proto.handoff.ask import build_prompt as ask_prompt
    from proto.executor import ExecOutcome

    old = head_funcs("proto/agent.py", ("_outcome_note", "_ask_prompt"), {})
    outcome = ExecOutcome(status="waiting_for_input", report="我需要课时", progress="做了一半",
                          missing=["课时"])
    msgs = [HumanMessage("帮我备课"), AIMessage("好的"), HumanMessage("第一课时")]
    a = old["_ask_prompt"]("quiz", "出题", outcome, msgs)
    b = ask_prompt("quiz", "出题", outcome, msgs)
    check("试答 prompt 逐字相同", a == b, f"旧 {len(a)} / 新 {len(b)}")
    if a != b:
        for x, y in zip(a.splitlines(), b.splitlines()):
            if x != y:
                print("      首个不同:", repr(x[:70]), "≠", repr(y[:70]))
                break

    print("\n" + "═" * 70)
    print(f"{'全部逐字相同' if not FAIL else '有不一致'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for n in FAIL:
        print(f"  ❌ {n}")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
