"""跑批入口。

    uv run python -m evals.run --tag baseline                # 全部 case × 3 遍
    uv run python -m evals.run --tag smoke --tier smoke      # 只跑 smoke 档
    uv run python -m evals.run --tag x --only s01,s04 --reps 5

每个 run 起一个**子进程**（理由见 `_child.py` 的模块 docstring）。
跑完自动出报告：`evals/runs/<tag>/report.md`。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

if hasattr(sys.stdout, "reconfigure"):  # Windows：不设中文/符号输出会撞 GBK
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # langgraph-lab/
CASES_DIR = HERE / "cases"
# 🔴 **不要自己算 `HERE / "runs"`**（2026-10-30 踩过）：冻结后 `HERE` 在 `_internal/` 里，
# 于是 `_append_trace` 把 trace 附录写进了 `_internal/evals/runs/` —— 用户永远看不到，
# 收回来的一堆 `_run.md` 全都缺了附录。**路径只有 `harness` 一个来源。**
from evals.harness import RUNS_DIR  # noqa: E402


# ──────────────────────────── 载入 ────────────────────────────


def load_cases() -> list[tuple[Path, dict]]:
    out: list[tuple[Path, dict]] = []
    for p in sorted(CASES_DIR.glob("*.yaml")):
        out.append((p, yaml.safe_load(p.read_text(encoding="utf-8"))))
    return out


# ──────────────────────────── 跑一个 run ────────────────────────────


def run_one(case_path: Path, case: dict, rep: int, tag: str, timeout: int) -> dict:
    out_path = RUNS_DIR / tag / f"{case['id']}__r{rep}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "PYTHONUTF8": "1",  # Windows：不设中文输出会撞 GBK
        # 时间戳只到秒，并发子进程会撞同一个日志文件 —— 用 tag 区分
        "PROTO_LOG_TAG": f"{case['id']}-r{rep}",
        # 🔴 **跑批的日志是噪音** —— 别混进给人看"真会话"的 `logs/`（实测：那里 2309 个
        #    文件里 826 个是噪音）。`tmp/` 本来就在 `.gitignore` 里。〔`trace` 沿革 32〕
        "PROTO_LOG_DIR": "tmp/logs",
    }
    t0 = time.time()
    try:
        p = subprocess.run(
            [sys.executable, "-m", "evals._child", str(case_path), str(rep), str(out_path)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "case": case["id"], "rep": rep, "turns": [], "turns_done": 0,
            "usage": {}, "artifact_writes": [], "artifact_ids": [], "artifact_final": {},
            "fault": {}, "error": f"子进程超时（>{timeout}s）", "wall_s": round(time.time() - t0, 1),
            "child_stderr": "",
        }

    if p.returncode != 0 or not out_path.exists():
        return {
            "case": case["id"], "rep": rep, "turns": [], "turns_done": 0,
            "usage": {}, "artifact_writes": [], "artifact_ids": [], "artifact_final": {},
            "fault": {}, "error": f"子进程退出码 {p.returncode}，没产出结果文件",
            "wall_s": round(time.time() - t0, 1),
            "child_stderr": (p.stderr or "")[-2000:],
        }

    result = json.loads(out_path.read_text(encoding="utf-8"))
    result["child_stderr"] = (p.stderr or "")[-2000:]
    return result


# ──────────────────────────── 判定 ────────────────────────────


def _pick_turn(result: dict, at: int | None) -> dict | None:
    turns = result.get("turns") or []
    if not turns:
        return None
    if at is None:
        return turns[-1]
    return turns[at - 1] if 1 <= at <= len(turns) else None


def evaluate(case: dict, result: dict) -> list[dict]:
    """跑完 case 的硬断言 + 软断言。**判定失败算不通过，不算通过。**"""
    from evals.checks import CHECKS

    out: list[dict] = []

    for spec in case.get("checks") or []:
        spec = dict(spec)
        name = spec.pop("check")
        at = spec.pop("at", None)
        turn = _pick_turn(result, at)
        if turn is None:
            out.append({"check": name, "at": at, "soft": False, "pass": False,
                        "detail": f"第 {at} 轮没跑到（前面就中断了）"})
            continue
        fn = CHECKS.get(name)
        if fn is None:
            out.append({"check": name, "at": at, "soft": False, "pass": False,
                        "detail": f"⚠️ 没有这个断言：{name}（可用：{sorted(CHECKS)}）"})
            continue
        try:
            ok, detail = fn(result, turn, **spec)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"⚠️ 断言自身报错：{type(e).__name__}: {e}"
        out.append({"check": name, "at": at, "soft": False, "pass": bool(ok), "detail": detail})

    if case.get("soft"):
        from evals.judge import RUBRICS

        for spec in case["soft"]:
            spec = dict(spec)
            name = spec.pop("check")
            at = spec.pop("at", None)
            turn = _pick_turn(result, at)
            fn = RUBRICS.get(name)
            if turn is None or fn is None:
                out.append({"check": name, "at": at, "soft": True, "pass": False,
                            "detail": f"第 {at} 轮没跑到，或没有这个 rubric：{name}"})
                continue
            try:
                ok, detail = fn(result, turn, **spec)
            except Exception as e:  # noqa: BLE001
                ok, detail = False, f"⚠️ 评分自身报错：{type(e).__name__}: {e}"
            out.append({"check": name, "at": at, "soft": True, "pass": bool(ok), "detail": detail})

    return out


# ──────────────────────────── 主流程 ────────────────────────────


def _append_trace(result: dict, tag: str) -> None:
    """把该 run 的**原始 trace** 作为附录并进它自己的 `_run.md`（一份文件读完）。

    顺序：概览 → 逐轮全记录（子进程写）→ 软判断留档（`judge._log` 追加）→ 本附录。
    """
    src = result.get("trace_log")
    if not src or not Path(src).exists():
        return
    d = RUNS_DIR / tag / "artifacts" / f"{result['case']}__r{result['rep']}"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "_run.md").open("a", encoding="utf-8") as fh:
        fh.write(
            "\n\n## 附录 · 原始 trace\n\n"
            "执行器每一轮、被推回去时的提示词、判定逐条理由、以及软判断的逐字 "
            "prompt/response，都在这里。\n\n"
            "````\n"  # ⚠️ 四个反引号：内容里本身就有 ``` （技能指令、产物正文）
        )
        fh.write(Path(src).read_text(encoding="utf-8", errors="replace"))
        fh.write("\n````\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="跑批：case × N 遍")
    ap.add_argument("--tag", required=True, help="本次跑批的名字（结果落在 runs/<tag>/）")
    ap.add_argument("--reps", type=int, default=3, help="每个 case 跑几遍（默认 3）")
    ap.add_argument("--workers", type=int, default=4, help="并发几个子进程（默认 4）")
    ap.add_argument("--only", help="只跑这些 case id，逗号分隔")
    ap.add_argument("--tier", help="只跑这个档位（如 smoke）")
    ap.add_argument("--timeout", type=int, default=1200, help="单个 run 的超时秒数")
    ap.add_argument("--no-report", action="store_true")
    args = ap.parse_args()

    cases = load_cases()
    if args.only:
        want = {x.strip() for x in args.only.split(",")}
        cases = [(p, c) for p, c in cases if c["id"] in want]
    if args.tier:
        cases = [(p, c) for p, c in cases if c.get("tier") == args.tier]
    if not cases:
        raise SystemExit("没有匹配的 case")

    jobs = [(p, c, r) for p, c in cases for r in range(1, args.reps + 1)]
    print(f"跑批 tag={args.tag}：{len(cases)} 个 case × {args.reps} 遍 = {len(jobs)} 个 run")
    print(f"并发 {args.workers}｜结果落 {RUNS_DIR / args.tag}")
    print(f"case：{'、'.join(c['id'] for _, c in cases)}\n")

    results: dict[tuple[str, int], dict] = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {
            pool.submit(run_one, p, c, r, args.tag, args.timeout): (c["id"], r)
            for p, c, r in jobs
        }
        for i, f in enumerate(as_completed(futs), 1):
            cid, rep = futs[f]
            try:
                res = f.result()
            except Exception as e:  # noqa: BLE001
                res = {"case": cid, "rep": rep, "turns": [], "turns_done": 0, "usage": {},
                       "artifact_writes": [], "artifact_ids": [], "artifact_final": {},
                       "fault": {}, "error": f"{type(e).__name__}: {e}", "wall_s": 0}
            results[(cid, rep)] = res
            flag = "❌" if res.get("error") else "✓"
            print(f"  [{i}/{len(jobs)}] {flag} {cid} r{rep}　{res.get('wall_s', 0)}s"
                  f"　轮次={res.get('turns_done', 0)}"
                  f"　token={(res.get('usage') or {}).get('total_tokens', 0)}")

    # 判定 + 落盘
    # 软判断的输入输出留档：判错了要能看出是"模型判错"还是"材料喂错了"
    from evals import judge

    judge.RUNS_DIR = RUNS_DIR / args.tag

    case_by_id = {c["id"]: c for _, c in cases}
    lines: list[dict] = []
    for (cid, rep), res in sorted(results.items()):
        checks = evaluate(case_by_id[cid], res)
        res["checks"] = checks
        _append_trace(res, args.tag)
        lines.append(res)

    # ⚠️ **缩进的多行 JSON**，不是 JSONL —— 用户要能直接打开看
    # （2026-10-29：JSONL 一行几十万字，编辑器打不开；人读的入口是 `_run.md`，
    #   这份是"整批结果"的结构化视图）
    out_file = RUNS_DIR / args.tag / "results.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(lines, ensure_ascii=False, indent=2), encoding="utf-8")

    n_pass = sum(1 for r in lines if r["checks"] and all(c["pass"] for c in r["checks"]))
    print(f"\n跑完：{len(lines)} 个 run，{time.time() - t0:.0f}s")
    print(f"全绿的 run：{n_pass}/{len(lines)}")
    print(f"结果：{out_file}")

    if not args.no_report:
        from evals.report import build

        md = build(args.tag)
        print(f"报告：{md}")


if __name__ == "__main__":
    main()
