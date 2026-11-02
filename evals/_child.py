"""跑批子进程：跑**一个 case 的一遍**，把完整快照写成 JSON。

    python -m evals._child <case.yaml> <rep> <out.json>

**为什么必须是子进程**（不是洁癖，是必须）：

| 状态 | 存在哪 | 不隔离的后果 |
|---|---|---|
| `skills.ARTIFACTS` | **模块级全局 dict** | 上轮的产物带进下轮，产物 ID 递增、`_recover_artifacts` 扫描范围越来越大 |
| `trace.LOG_FILE` | **进程级**，导入时开文件 | 所有 run 混进同一个日志 |
| `config.get_model` | `lru_cache` | 跨 run 复用同一个模型实例 |
| 崩了 | 整个进程 | 一个 case 炸掉整批 |

顺带白拿：并发（run.py 起多个子进程）和崩溃隔离。

⚠️ **装配 / 快照 / 落盘不在这里** —— 那些是交互式 CLI 和跑批**共用**的，
在 `evals/harness.py`（本文件只负责"跑批子进程"这一个角色：case → run → JSON）。

## 三个"值导入"的坑（不写下来下次一定踩）

`agent.py` / `executor.py` / `llm.py` 都是 `from proto.config import get_model`
这种**值导入** —— 改 `proto.config.get_model` **不生效**，得改**它们各自命名空间里的那个名字**。
`SKILLS` 同理：`from proto.skills import SKILLS` 是值导入，
但 `delegate` 和 `build_agent` 都读 `proto.agent` 的模块全局，所以 patch `proto.agent.SKILLS` 一处就够。
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import yaml
from langchain_core.messages import HumanMessage

from evals.harness import dump_artifacts, dump_run_record, install, snapshot


# ──────────────────────────── 主流程 ────────────────────────────


def run_case(case: dict, rep: int, out_dir: Path | None = None) -> dict:
    from proto import trace
    from proto.artifacts import ARTIFACTS

    inst = install(case)

    from proto.graph import build

    graph = build()
    config = {"configurable": {"thread_id": f"{case['id']}-r{rep}"}}

    turns: list[dict] = []
    seen = 0
    error: str | None = None
    t0 = time.time()

    try:
        for i, user in enumerate(case["turns"], 1):
            t1 = time.time()
            init: dict = {"messages": [HumanMessage(user)], "log": []}
            # 🔴 **平台造路**（2026-11-02）：case 可以往**第 1 轮**的初始 state 里注入 Task
            # （典型是 `stage: waiting_for_input` 的），于是"答复投递 / 重入"那条路径
            # **必然**被走到 —— 不用再指望"执行器先问一轮"。
            #
            # 为什么必须这么干：指望模型走某条路 = 把 case 的有效性押在**它的自由选择**上，
            # 那条断言必然脆（`t3-split-answers` 的 `run_task_count_over: 3` 就是这么挂的：
            # 主 agent 自己把信息收齐了才派发，执行器一次就干完，路径压根没被走到）。
            # 判据见 `checks.py` 顶部。
            if i == 1 and case.get("initial_tasks"):
                init["tasks"] = case["initial_tasks"]
            state = graph.invoke(init, config)
            turns.append(snapshot(state, user, seen, i, round(time.time() - t1, 1)))
            seen = len(state.get("messages") or [])
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"

    return {
        "case": case["id"],
        "rep": rep,
        "turns": turns,
        "turns_done": len(turns),
        "usage": inst["counter"].snapshot(),
        "per_call_tokens": inst["counter"].per_call,
        "artifact_writes": inst["writes"],
        "run_tasks": inst["run_tasks"],
        "artifact_ids": list(ARTIFACTS.keys()),
        # 正文全文一并带上：`artifact_no_placeholder` / `artifact_html_complete` 这类
        # **纯代码**断言要扫正文，而快照里只有字数的话它们无从判起。
        "artifact_final": {
            k: {
                "title": v["title"],
                "summary": v.get("summary", ""),
                #: 产出方**声明的**类型（`md` / `html`）—— 断言按它判"哪些算网页"，
                #: 不按内容嗅探（见 `checks.check_artifact_html_complete`）
                "type": v.get("type", ""),
                "ext": v.get("ext", ""),
                "chars": len(v["content"]),
                "content": v["content"],
            }
            for k, v in ARTIFACTS.items()
        },
        "fault": inst["fault"],
        "error": error,
        "wall_s": round(time.time() - t0, 1),
        #: 本次 run 的原始 trace 文件 —— 父进程判完软断言后把它作为附录并进 `_run.md`
        "trace_log": str(trace.LOG_FILE),
    }


def main() -> None:
    case_path, rep, out_path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    out = Path(out_path)
    case = yaml.safe_load(Path(case_path).read_text(encoding="utf-8"))
    result = run_case(case, rep, out_dir=out.parent)

    # 🔴 产物原文 + 逐轮对话落盘 —— 给人工审计用（判定看不到的东西，人能看到）
    dump_artifacts(out.parent, case["id"], rep, result)
    dump_run_record(out.parent, case["id"], rep, result)

    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
