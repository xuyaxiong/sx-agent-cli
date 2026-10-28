"""诊断执行器行为。

    uv run python debug_executor.py                  # lesson-prep 连跑 3 次
    uv run python debug_executor.py 1 courseware     # 互动课件，看消息轨迹
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

from proto.config import describe, get_model
from proto.skills import ARTIFACTS, SKILLS, finish, read_artifact, run_skill, write_artifact

PARAMS = {"年级": "三年级", "学科": "数学", "课题": "分数的初步认识"}
PARAMS_CW = {"年级": "三年级", "学科": "数学", "难度": "基础"}


def trace(skill_name: str, params: dict) -> None:
    """把执行器的完整消息轨迹打出来。"""
    skill = SKILLS[skill_name]
    agent = create_agent(
        get_model(),
        tools=[write_artifact, read_artifact, finish],
        system_prompt=skill.system_prompt,
        name=f"exec_{skill_name}",
    )
    param_text = "\n".join(f"- {k}：{v}" for k, v in params.items() if v)
    resp = agent.invoke({"messages": [HumanMessage(f"任务参数：\n{param_text}")]})

    print(f"\n── {skill_name} 消息轨迹 ──")
    for i, m in enumerate(resp["messages"]):
        calls = getattr(m, "tool_calls", None) or []
        names = [c.get("name") for c in calls]
        content = str(m.content)
        head = content[:90].replace("\n", " ")
        print(f"  [{i}] {type(m).__name__:<14} len={len(content):<6} calls={names}")
        if names:
            for c in calls:
                print(f"        args={str(c.get('args'))[:160]}")
        elif content:
            print(f"        {head!r}")
    print(f"  产物库: {[(k, v['title'], len(v['content'])) for k, v in ARTIFACTS.items()]}")


def repeat(skill_name: str, params: dict, n: int) -> None:
    """连跑 N 次，统计成功率。"""
    skill = SKILLS[skill_name]
    print(f"模型：{describe()}   {skill_name} 连跑 {n} 次\n")
    ok = 0
    for i in range(1, n + 1):
        before = set(ARTIFACTS)
        try:
            r = run_skill(skill, params)
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}] ❌ 异常 {type(e).__name__}: {e}")
            continue
        new = [k for k in ARTIFACTS if k not in before]
        body = len(ARTIFACTS[new[0]]["content"]) if new else 0
        good = r.status == "done" and bool(new) and bool(r.summary)
        ok += good
        print(
            f"  [{i}] {'✅' if good else '❌'} status={r.status:<11} "
            f"新产物={new or '（无）'} 正文={body}字  summary={r.summary[:50]!r}"
        )
    print(f"\n成功率：{ok}/{n}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    which = sys.argv[2] if len(sys.argv) > 2 else "lesson-prep"
    if which == "quiz":
        trace("quiz", PARAMS_CW)
    else:
        repeat(which, PARAMS, n)
