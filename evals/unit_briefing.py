"""单元级：**Task 的每个字段都要么渲染进交接书、要么在排除名单里**（不调 LLM）。

    uv run python -m evals.unit_briefing

**为什么要有它**（2026-10-29）：F15 的病根是"状态必须显式落在 Task 上"这条原则
**只靠人记得** —— 前两条原则（执行器无状态、重入走新 worker）是**结构性**的，
代码长成那样就自动满足；这一条是**纪律性**的，事故恰好出在它身上。

这条断言把它从"纪律"变成"机制"：造一个**所有字段都填满**的 Task，渲染，
逐个断言每个非排除字段的值都出现在结果里。**以后加字段忘了渲染，这里就红。**

> 有效性自检：把 `render_briefing` 里任意一块注释掉，本测试必须红
> （2026-10-29 手工验过：删掉 `progress` 那块 → 红）。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from proto.agent import render_briefing
from proto.skills import ARTIFACTS, store_artifact

#: **显式排除名单** —— 这些字段不进交接书，每一条都要有理由。
#: ⚠️ 加字段的人只有两条路：渲染它，或者在这里写清楚为什么不渲染。
EXCLUDED: dict[str, str] = {
    "id": "平台内部标识（台账用），执行器不需要",
    "skill": "能力名 —— 技能指令已经整份注入了，名字没有增量信息",
    "stage": "平台状态机内部字段；'这是重入'这层意思由【续跑提示】承载",
    "agent_tried": "布尔标记；它要传达的信息由 `agent_answer` 承载（置 True 时必然非空）",
}

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail else ""))


def _full_task() -> dict:
    """**所有字段都填满**的 Task —— 空字段测不出"漏渲染"。"""
    ARTIFACTS.clear()
    produced = store_artifact("《白桦》导学单", "课前预习用填空式导学单", "导学单正文" * 40)
    readable = store_artifact("《白桦》教案", "四年级下册诗歌教学", "教案正文" * 60)
    return {
        "id": "t12345678",
        "skill": "guided-learning",
        "task": "生成《白桦》导学单，课前预习用",
        "stage": "waiting_for_input",
        "artifacts": [produced],
        "refs": [readable],
        "report": "好的，我先设计学习目标……请问这份导学单用在哪？",
        "progress": "已完成学习目标与预习任务",
        "missing": ["当堂检测（需要课时才能写）"],
        "answers": ["课前预习", "填空形式"],
        "agent_answer": "用户之前说过是四年级",
        "agent_tried": True,
    }


def main() -> None:
    task = _full_task()
    text = render_briefing(task)

    print("① 每个非排除字段的值都要出现在交接书里")
    for key, value in task.items():
        if key in EXCLUDED:
            continue
        values = value if isinstance(value, list) else [value]
        missing = [v for v in values if str(v) not in text]
        check(f"`{key}` 渲染了", not missing, f"没出现：{missing}" if missing else "")

    print("\n② 排除名单里的字段**不该**出现（双向：要么渲染、要么显式排除）")
    for key in EXCLUDED:
        value = str(task[key])
        # `stage` 的值是 waiting_for_input，正文里不该出现这个英文状态名
        ok = value not in text
        check(f"`{key}` 没泄漏", ok, "" if ok else f"出现了：{value!r}")

    print("\n③ 重入该有的提示语都在")
    check("说了这是上一轮的情况", "上一轮的情况" in text)
    check("说了不要重复已完成的工作", "不要重复已完成的工作" in text)
    check("说了冲突以后面的为准", "以后面的为准" in text)
    check("说了在已有产物上补全", "在它基础上补全" in text)

    print("\n④ 首跑不该出现「续跑」相关的话（否则它会以为有前情）")
    fresh = {k: ([] if isinstance(v, list) else "") for k, v in task.items()}
    fresh.update({"task": "生成《白桦》导学单", "stage": "running", "id": "t1", "skill": "s"})
    fresh_text = render_briefing(fresh)
    check("首跑只有任务", fresh_text.strip() == "【任务】\n生成《白桦》导学单", repr(fresh_text))
    check("首跑没有续跑提示", "续跑提示" not in fresh_text)

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
