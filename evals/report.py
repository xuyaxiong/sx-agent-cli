"""把 `results.json` 汇总成一份能直接看的 markdown。

    uv run python -m evals.report <tag>

出四样东西：
1. **通过率总表** —— 按通过率升序，问题排前面
2. **逐条断言通过率** —— **通过率不是 100% 的就是 flaky**，那才是要修的
3. **指标表** —— 主上下文体积 / 产物体积 / token / 耗时（"效果"的量纲）
4. **失败详情** —— 每条失败的 detail 原样列出，不用翻 JSON
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 🔴 **不要自己算 `HERE / "runs"`** —— 路径只有 `harness` 一个来源（理由见 `run.py` 同处注释）
from app.runtime import RUNS_DIR  # noqa: E402

#: **测试有效性断言** —— 这些不绿，说明这次跑**根本没测到东西**，
#: 同一 case 里其他断言的"通过"是假阴性（如：并发没触发 → "没撞车"毫无意义）
VALIDITY_CHECKS = {"artifact_writes_multithreaded", "fault_fired"}


def _load(tag: str) -> list[dict]:
    """读整批结果。

    ⚠️ 是**缩进的多行 JSON 数组**，不是 JSONL —— 用户要能直接打开看
    （2026-10-29：JSONL 一行几十万字，编辑器打不开）。
    """
    p = RUNS_DIR / tag / "results.json"
    if not p.exists():
        raise SystemExit(f"没有这个跑批：{p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _med(xs: list[float]) -> str:
    return f"{statistics.median(xs):.0f}" if xs else "—"


def build(tag: str) -> Path:
    runs = _load(tag)
    case_ids = sorted({r["case"] for r in runs})
    by_case: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        by_case[r["case"]].append(r)

    out: list[str] = []
    out.append(f"# 跑批报告 · `{tag}`\n")
    out.append(f"- run 总数：**{len(runs)}**（{len(case_ids)} 个 case）")
    errs = [r for r in runs if r.get("error")]
    out.append(f"- 整轮炸掉：**{len(errs)}**")
    total_tok = sum((r.get("usage") or {}).get("total_tokens", 0) for r in runs)
    total_s = sum(r.get("wall_s", 0) for r in runs)
    out.append(f"- 累计 token：**{total_tok:,}**｜累计耗时：**{total_s / 60:.0f} 分钟**（串行口径）")
    out.append("")
    out.append(
        "> 📂 **每个 run 的完整记录：`artifacts/<case>__r<rep>/_run.md`（一份文件读完）**\n"
        "> 概览 → 逐轮全记录（用户输入 / 主 agent 输出 / 工具调用完整参数 / 工具返回 / "
        "平台注入的台账 / Task 台账含产出与输入引用 / 执行器原话）→ 软判断留档（逐字 "
        "prompt 与 response）→ 原始 trace 附录（执行器每一轮、推回去的提示词、试答的决策记录）。\n"
        "> 同目录还有每个产物一个文件（HTML 原样落 `.html`，可直接打开）。\n"
        ">\n"
        "> **判「完整性」以产物原文为准** —— 中间隔着任何「转述 / 截断」的一层都不算\n"
        "> （2026-11-05 判定退休前，这一条是冲着「判定被预算截断后给的是推测」写的；\n"
        "> 那次事故见 `docs/discussion-log.md` F1，事实仍成立）。\n"
    )

    # ── 1. 通过率总表 ──
    out.append("## 一、通过率总表（低 → 高）\n")
    out.append("| case | 全绿 run | 通过率 | 断言 | 备注 |")
    out.append("|---|---|---|---|---|")
    rows = []
    for cid in case_ids:
        rs = by_case[cid]
        green = sum(1 for r in rs if r["checks"] and all(c["pass"] for c in r["checks"]))
        nchk = len(rs[0]["checks"]) if rs and rs[0]["checks"] else 0
        note = ""
        if any(r.get("error") for r in rs):
            note = "⚠️ 有 run 整轮炸掉"
        rows.append((green / len(rs), cid, green, len(rs), nchk, note))
    for rate, cid, green, n, nchk, note in sorted(rows):
        mark = "🔴" if rate == 0 else ("⚠️" if rate < 1 else "✅")
        out.append(f"| {mark} `{cid}` | {green}/{n} | {rate:.0%} | {nchk} | {note} |")
    out.append("")

    # ── 2. 逐条断言通过率（flaky 榜）──
    out.append("## 二、逐条断言通过率（**不是 100% 的就是 flaky**）\n")
    agg: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for r in runs:
        for c in r.get("checks") or []:
            agg[(r["case"], c["check"])].append(c["pass"])
    flaky = [(sum(v) / len(v), k, v) for k, v in agg.items() if sum(v) < len(v)]
    if not flaky:
        out.append("✅ **全部断言 100% 通过。**\n")
    else:
        out.append("| case | 断言 | 通过 | 类型 | 说明 |")
        out.append("|---|---|---|---|---|")
        for rate, (cid, name), v in sorted(flaky):
            kind = "🔬 测试有效性" if name in VALIDITY_CHECKS else ("软断言" if _is_soft(runs, cid, name) else "硬断言")
            out.append(f"| `{cid}` | `{name}` | {sum(v)}/{len(v)} | {kind} | "
                       f"{'**这条不绿，同 case 其他断言的通过是假阴性**' if name in VALIDITY_CHECKS else ''} |")
        out.append("")

    # ── 3. 指标表 ──
    out.append("## 三、指标（中位数 / 最大）\n")
    out.append("| case | 主上下文·消息 | system prompt | 产物正文 | LLM 调用 | token | 单 run 秒 |")
    out.append("|---|---|---|---|---|---|---|")
    for cid in case_ids:
        rs = [r for r in by_case[cid] if r.get("turns")]
        if not rs:
            out.append(f"| `{cid}` | — | — | — | — | — | — |")
            continue
        last = [r["turns"][-1] for r in rs]
        ctx = [t["main_ctx_chars"] for t in last]
        sysp = [t.get("sys_prompt_chars", 0) for t in last]
        art = [t["artifacts_chars"] for t in last]
        calls = [(r.get("usage") or {}).get("llm_calls", 0) for r in rs]
        toks = [(r.get("usage") or {}).get("total_tokens", 0) for r in rs]
        secs = [r.get("wall_s", 0) for r in rs]
        out.append(
            f"| `{cid}` | {_med(ctx)} / {max(ctx)} | {_med(sysp)} | {_med(art)} / {max(art)} "
            f"| {_med(calls)} | {_med(toks)} | {_med(secs)} |"
        )
    out.append("")
    out.append(
        "> ⚠️ **`主上下文·消息` 不含 system prompt**（`create_agent(system_prompt=...)` 不落 state，"
        "量不到）—— 口径与 `proto/__main__` 一致，**跟历史数据可比**。\n"
        "> 真实上下文 = 两者相加。**system prompt 是一笔固定开销**，"
        "隔离场景里它可能比消息还大，别忘了算。\n"
    )

    # ── 4. 失败详情 ──
    out.append("## 四、失败详情\n")
    any_fail = False
    for cid in case_ids:
        for r in sorted(by_case[cid], key=lambda x: x["rep"]):
            bad = [c for c in (r.get("checks") or []) if not c["pass"]]
            if not bad and not r.get("error"):
                continue
            any_fail = True
            out.append(f"### `{cid}` · r{r['rep']}\n")
            if r.get("error"):
                out.append(f"**整轮炸掉**：\n```\n{r['error'][:1500]}\n```\n")
            for c in bad:
                tag_at = f"（第 {c['at']} 轮）" if c.get("at") else ""
                out.append(f"- ❌ `{c['check']}`{tag_at} — {c['detail']}")
            if r.get("turns"):
                out.append(f"\n<details><summary>最后一轮的主 agent 回复</summary>\n")
                out.append(f"\n```\n{(r['turns'][-1].get('reply') or '')[:1200]}\n```\n</details>\n")
            out.append("")
    if not any_fail:
        out.append("✅ 没有失败。\n")

    path = RUNS_DIR / tag / "report.md"
    path.write_text("\n".join(out), encoding="utf-8")
    return path


def _is_soft(runs: list[dict], cid: str, name: str) -> bool:
    for r in runs:
        if r["case"] != cid:
            continue
        for c in r.get("checks") or []:
            if c["check"] == name:
                return bool(c.get("soft"))
    return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法：uv run python -m evals.report <tag>")
    print(build(sys.argv[1]))
