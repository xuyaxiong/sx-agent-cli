"""诊断：一次 `graph.invoke` 里，`prepare` / `finalize` 到底跑了几次？

现象（`runs/f7fix/*`）：第 2 轮开始，日志里 prepare/finalize 各出现 2 次，
第 3 轮 4 次。查清是图真的重跑，还是别的原因。

    uv run python -m experiments.probe_double_prepare
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from langchain_core.messages import HumanMessage

import proto.graph as G

CALLS: list[str] = []


def _wrap(name: str, fn):
    def inner(state):
        CALLS.append(f"{name}#{sum(1 for c in CALLS if c.startswith(name)) + 1}")
        print(f"   >>> {CALLS[-1]}　进入时 state.log={len(state.get('log') or [])} 条"
              f"　tasks={len(state.get('tasks') or [])} 个")
        r = fn(state)
        if isinstance(r, dict):
            print(f"       返回：log +{len(r.get('log') or [])} 条"
                  f"　tasks {len(r.get('tasks') or [])} 个"
                  f"　messages {len(r.get('messages') or [])} 条")
            for x in r.get("log") or []:
                print(f"         · {x[:70]}")
        return r

    return inner


def main() -> None:
    G.node_prepare = _wrap("prepare", G.node_prepare)
    G.node_finalize = _wrap("finalize", G.node_finalize)

    graph = G.build()
    cfg = {"configurable": {"thread_id": "probe-double"}}

    for i, text in enumerate(
        [
            "帮我出个随堂小测，人教版三年级数学《分数的初步认识》第一课时",
            "对了，我们班这周讲到哪了来着？",
        ],
        1,
    ):
        CALLS.clear()
        print(f"\n═══ 第 {i} 轮 invoke：{text}")
        st = graph.invoke({"messages": [HumanMessage(text)], "log": []}, cfg)
        print(f"   <<< 本轮节点调用序列：{CALLS}")
        print(f"   <<< 返回的 log 共 {len(st.get('log') or [])} 条")

    print("\n结论：看每轮的调用序列 —— prepare/finalize 各出现几次。")


if __name__ == "__main__":
    main()
