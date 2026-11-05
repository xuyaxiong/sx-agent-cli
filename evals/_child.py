"""跑批子进程：跑**一个 case 的一遍**，把完整快照写成 JSON。

    python -m evals._child <case.yaml> <rep> <out.json>

**为什么必须是子进程**（不是洁癖，是必须）：

| 状态 | 存在哪 | 不隔离的后果 |
|---|---|---|
| 产物库（`proto.artifacts`） | **模块级全局**（对外只有函数） | 上轮的产物带进下轮，产物 ID 递增、`_recover_artifacts` 扫描范围越来越大 |
| `trace.LOG_FILE` | **进程级**，导入时开文件 | 所有 run 混进同一个日志 |
| `config.get_model` | `lru_cache` | 跨 run 复用同一个模型实例 |
| 崩了 | 整个进程 | 一个 case 炸掉整批 |

顺带白拿：并发（run.py 起多个子进程）和崩溃隔离。

⚠️ **装配 / 快照 / 落盘不在这里** —— 那些是交互式 CLI 和跑批**共用**的，
在 `evals/harness.py`（本文件只负责"跑批子进程"这一个角色：case → run → JSON）。

## "值导入"的坑（不写下来下次一定踩）

`agent.py` / `executor.py` / `llm.py` 都是 `from proto.config import get_model`
这种**值导入** —— 改 `proto.config.get_model` **不生效**，得改**它们各自命名空间里的那个名字**。

⚠️ **技能表不在这个坑里**（R1 之后）：它没有"值导入"的机会了，因为对外只有
`registry.get()/all()` 这类函数，每次调用查当前表。换技能一律走
`registry.replace()/extend()/reset()` —— **不要再 patch 任何模块的 `SKILLS` 属性**。
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
    from proto import artifacts

    inst = install(case)

    from proto.graph import build, next_pending

    graph = build()
    config = {"configurable": {"thread_id": f"{case['id']}-r{rep}"}}

    turns: list[dict] = []
    seen = 0
    error: str | None = None
    t0 = time.time()

    prev_state: dict = {}
    try:
        for i, raw in enumerate(case["turns"], 1):
            t1 = time.time()
            # 一轮可以是**字符串**，也可以是 `{text, answer_to}` —— 后者表示
            # **这一句是在回答某个待答任务**（`solution.md` §0.65 Y1：标识由**通道**填，
            # 平台不猜"你在答谁"）。
            #   · `answer_to: <任务ID>` —— 明确指哪一条（注入的 Task 用这个，ID 是写死的）
            #   · `answer_to: "@waiting"` —— **上一个 state 里第一个待答任务**
            #     （和 CLI 的 `↩` 提示符**同一条规则**：一次摆一个、摆的是第一个）
            #   · `answer_to: "@waiting:<skill>"` —— 那个技能下第一个待答
            #     （多技能**并行**派发时"第一个"不确定，脚本得点名是哪个技能在等）
            if isinstance(raw, dict):
                user, answer_to = str(raw.get("text") or ""), str(raw.get("answer_to") or "")
            else:
                user, answer_to = str(raw), ""
            if answer_to.startswith("@waiting"):
                _skill = answer_to.split(":", 1)[1].strip() if ":" in answer_to else ""
                # ⚠️ **技能名写错 → 当场炸**（那是 case 写错了，静默下去这条 case 就悄悄变了）；
                #    **任务在、只是没在等 → 只记日志**（"执行器会不会提问"是模型的自由选择，
                #    条件性的待答不算写错 —— 真没投到，case 自己的断言会说话）。
                if _skill and not any(
                    t.get("skill") == _skill for t in (prev_state.get("tasks") or [])
                ):
                    raise ValueError(
                        f"{case['id']} 第 {i} 轮标了 `answer_to: {answer_to!r}`，"
                        f"但上一轮结束时**根本没有「{_skill}」这个技能的任务**（case 写错了）"
                    )
                _p = next_pending(prev_state, _skill)
                if _p is None:
                    print(f"⚠️ {case['id']} 第 {i} 轮：`{answer_to}` 没有对应的待答"
                          f"（这一轮**不投递**，当普通输入）")
                    answer_to = ""
                else:
                    answer_to = _p["id"]
            init: dict = {"messages": [HumanMessage(user)], "log": []}
            if answer_to:
                init["answer_to"] = answer_to
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
            snap = snapshot(state, user, seen, i, round(time.time() - t1, 1))
            if answer_to:
                snap["answer_to"] = answer_to   # 记录层如实标：这句是在答哪一条
            turns.append(snap)
            seen = len(state.get("messages") or [])
            prev_state = state
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
        "artifact_ids": artifacts.ids(),
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
            for k, v in artifacts.items()
        },
        # 两种注入的计数器**都要带出来**（2026-11-05 修）：`fault` 是 `structured` 那条路
        # （`safe_structured`），`stream_fault` 是流式那条路（`stream_agent`）——
        # 原来只带前者，于是用 `fault.stream` 注入的 case **明明故障发生了，
        # `check_fault_fired` 却报"触发 0 次"**（把"有效"报成"无效"，比没有更坏）。
        "fault": inst["fault"],
        "stream_fault": inst["stream_fault"],
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

    # 🔴 产物原文 + 逐轮对话落盘 —— 给人工审计用（主 agent 上下文里没有的东西，人能看到）
    dump_artifacts(out.parent, case["id"], rep, result)
    dump_run_record(out.parent, case["id"], rep, result)

    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
