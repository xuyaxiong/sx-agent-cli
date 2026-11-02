"""单元级：待答投递必须**累积历次答复**（不调 LLM）。

    uv run python -m evals.unit_feed_waiting

**为什么要有它**（2026-10-29 人在环实测暴露）：

执行器是隔离上下文、**用完即弃** —— 每次投递都是一次全新的 `run_task`。
`_feed_waiting` 原来只把**最新一条**用户消息拼进提示词，于是用户"一次答一个"时：

    问「用途」→ 答「课前预习」→ 问「填写形式」→ 答「A」→ **又问一遍「用途」**

答第二个就把第一个冲掉了，用户被同一个问题问两遍。**脚本化跑批测不出来**：
预置答复是一句话把两问都答了（"…用在课前预习，填空形式…"），恰好绕开了它。

这里把 `run_task` 换成记录器，直接看**待投递的提示词里有什么** —— 确定性、毫秒级、
不依赖模型是否"一次只问一个"。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from langchain_core.messages import HumanMessage

import proto.executor
from proto import graph

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail else ""))


class _Waiting:
    """假的 ExecOutcome：投递后仍待答（不关心内容，只看提示词）。"""

    status = "waiting_for_input"
    artifacts: list[str] = []
    report = "（还需要信息）"
    progress = ""
    missing: list[str] = []


def _capture() -> list[tuple[str, list | None]]:
    """把 `run_task` 换成记录器，返回它每次收到的 **（提示词, 用户原话）**。

    `_feed_waiting` 里是**函数内** `from proto.executor import run_task`，
    所以改模块属性就改得到（值导入的坑，见 `_child.py` 顶部）。
    """
    seen: list[tuple[str, list | None]] = []

    def fake(skill, briefing: str = "", user_turns=None):  # noqa: ANN001, ANN202
        seen.append((briefing, user_turns))
        return _Waiting()

    proto.executor.run_task = fake  # type: ignore[assignment]
    return seen


def main() -> None:
    print("① 用户第二次答复时，第一次的答复必须还在提示词里")
    seen = _capture()
    state = {
        "messages": [HumanMessage("篇幅一页")],
        "tasks": [
            {
                "id": "t1",
                "skill": "guided-learning",
                "task": "生成一份导学单",
                "stage": "waiting_for_input",
                "artifacts": [],
                "refs": [],
                "answers": ["课前预习"],
                "agent_answer": "",
            }
        ],
    }
    graph._feed_waiting(state)
    prompt = seen[0][0] if seen else ""
    check("投递发生了", bool(seen))
    check("带上本次答复", "篇幅一页" in prompt, prompt[-200:] if prompt else "（没投递）")
    check("**也带上历次答复**（这条是回归点）", "课前预习" in prompt, prompt[-200:] if prompt else "")

    print("\n② 主 agent 之前补的信息也要带上（同类问题：用完即弃会被冲掉）")
    seen = _capture()
    state["tasks"][0]["agent_answer"] = "用户之前说过是三年级"
    graph._feed_waiting(state)
    prompt = seen[0][0] if seen else ""
    check("带上 agent_answer", "用户之前说过是三年级" in prompt, prompt[-240:] if prompt else "")

    print("\n③ 可读产物清单照旧要带上（否则续跑时执行器看不见前序产物）")
    from proto.artifacts import ARTIFACTS, store_artifact

    ARTIFACTS.clear()
    readable = store_artifact("《白桦》教案", "四年级诗歌教学", "md", "教案正文" * 20)
    seen = _capture()
    state["tasks"][0]["refs"] = [readable]
    graph._feed_waiting(state)
    prompt = seen[0][0] if seen else ""
    check("可读产物进了交接书", readable in prompt and "《白桦》教案" in prompt, prompt[-260:])

    print("\n④ 投递的提示词**就是** `render_briefing(rec)`（三条路径共用同一份交接书）")
    from proto.agent import render_briefing

    seen = _capture()
    state["tasks"][0].update(
        {
            "progress": "已完成学习目标",
            "missing": ["当堂检测"],
            "report": "我先设计了目标……请问用在哪？",
        }
    )
    graph._feed_waiting(state)
    prompt = seen[0][0] if seen else ""
    expect = render_briefing({**state["tasks"][0], "answers": ["课前预习", "篇幅一页"]})
    check("提示词 == render_briefing(投递后的 Task)", prompt == expect, f"实际：\n{prompt[:400]}")
    check("上一轮的进度也带上了", "已完成学习目标" in prompt)
    check("它上次问过什么也带上了", "请问用在哪" in prompt)

    print("\n⑤ 用户原话跟着 Task 走（快照在 Task 上 → 这条重入路径**自动**带上）")
    seen = _capture()
    state["tasks"][0]["user_turns"] = [
        {"n": 1, "text": "帮我备一节三年级语文《荷花》的课，第一课时，直接生成不用再问我。"},
    ]
    graph._feed_waiting(state)
    prompt, turns = seen[0] if seen else ("", None)
    check("清单进了交接书", "第 1 轮" in prompt and "直接生成不用再问我" in prompt, prompt[-260:])
    # 🔴 交接书里只有清单，**全文走 `read_user_turn`** —— 所以原话必须一路传到 run_task
    check("原话也传给了执行器（read_user_turn 要用）", bool(turns), "" if turns else "传了个空的")

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
