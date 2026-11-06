"""单元级：`store_artifact` 的 **ID 竞态**。

## 曾经的窗口（已闭合，这里留着防回归）

`store_artifact` 原来用 `len(ARTIFACTS) + 1` 生成 ID —— **先查后写**，两步之间有窗口：

    aid = f"art_{len(ARTIFACTS) + 1:03d}"   # ① 算
    ARTIFACTS[aid] = {...}                  # ② 写

两个执行器同时算 ① 会拿到**同一个 ID**，② 后者覆盖前者 —— 产物**静默丢失**。

**2026-10-30 已修**：改成 `itertools.count()`（`next()` 在 GIL 下是原子的）。

> 📌 **修它的直接原因是它当场复发了**：给 `store_artifact` 加"当场落盘"时，
> ① 和 ② 之间**多插了两步**（嗅探文件类型、拼文件名），窗口被撑开 ——
> 本测试当场从 🟡"没撞上"翻成 🔴"**撞了 18577 次**"。
> 也就是说：**这个测试抓到了一个真实引入的回归**，不是纸面风险。

## 为什么必须单测

**端到端跑不到它。** 实测（`evals/runs/baseline/e13-*`）：3 个并行 delegate 的写入时间戳
间隔 **4–205 秒**（每次写入前面都有几十秒的 LLM 生成）。窗口在**微秒级**。
（我一开始写了个"多线程"有效性断言，它绿了 —— 那是**假阳性**：多线程 ≠ 同时。）

这里用 `threading.Barrier` 把线程卡在同一起跑线，再用 `setswitchinterval` 把 GIL
切换频率拉满 —— **放大器，不是被测代码的一部分**。

## 结论靠什么站得住

**对照组**：把窗口人为撑开（两步之间 `time.sleep(0)` 主动让出 GIL），
看这个测试**会不会报警**。对照组不炸，就说明测试根本没有探测能力，
"没撞上"的结论也就不值钱。

> ⚠️ **现在"原样没撞上"的含义变了**：不再是"窗口太窄、撞不上"，
> 而是"**发号器结构上就不会重号**"。所以这条测试现在的价值是**防回归** ——
> 谁把发号器改回 `len(ARTIFACTS) + 1`，或者往两步之间再插活，这里必须红。

    uv run python -m evals.unit_artifact_race

不调 LLM，秒级出结果。
"""

from __future__ import annotations

import ast
import io
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from proto import artifacts
from proto.artifacts import store_artifact

ROOT = Path(__file__).resolve().parent.parent


def _py_files() -> list[Path]:
    out: list[Path] = []
    for d in ("proto", "evals"):
        out += [p for p in (ROOT / d).rglob("*.py") if "__pycache__" not in p.parts]
    return sorted(out)


def _tree(p: Path) -> ast.Module:
    return ast.parse(io.open(p, encoding="utf-8").read(), str(p))


THREADS = 8
ROUNDS = 3000

sys.setswitchinterval(1e-6)  # 放大器：默认 5ms 太粗，压到 1µs


def hammer(store, label: str) -> tuple[int, int]:
    """N 个线程同一起跑线猛写。返回（写入次数, 撞掉的次数）。"""
    barrier = threading.Barrier(THREADS)
    out: list[str] = []
    lock = threading.Lock()

    def worker(tid: int) -> None:
        barrier.wait()
        local = [store(f"t{tid}-{i}", "梗概", "md", "x") for i in range(ROUNDS)]
        with lock:
            out.extend(local)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return len(out), len(out) - len({*out})


def main() -> None:
    print(f"线程 {THREADS} × 每个写 {ROUNDS} 次 = {THREADS * ROUNDS} 次写入\n")

    # ── 被测：原样的 store_artifact ──
    artifacts.clear()
    n, lost = hammer(store_artifact, "原样")
    print(f"【原样】     写入 {n}｜实存 {artifacts.count()}｜**撞掉 {lost}**")

    # ── 对照：窗口撑开（只为验证这个测试有探测能力，不是在测产品代码）──
    D: dict = {}

    def wide(title: str, summary: str, file_type: str, content: str) -> str:
        aid = f"art_{len(D) + 1:03d}"
        time.sleep(0)  # 主动让出 GIL —— 把微秒窗口撑到肉眼可见
        D[aid] = {"title": title, "summary": summary, "content": content}
        return aid

    n2, lost2 = hammer(wide, "窗口撑开")
    print(f"【对照·撑开】 写入 {n2}｜实存 {len(D)}｜**撞掉 {lost2}**")
    print()

    if lost2 == 0:
        print("⚠️ **对照组也没炸 —— 这个测试没有探测能力，上面的'没撞上'不算数。**")
        return

    # ── ② 产物库的**接口边界**（2026-11-03 审核收口）──
    #
    # 这张表原来叫 `ARTIFACTS` 且**公开**，于是 9 个模块直接读写它 ——
    # "换真存储（表 / 按会话分库）"就成了 9 处散点改造，谁漏一处谁静默读旧库。
    # 现在对外只有函数（`get/has/ids/items/count/chars/clear`），表本身私有。
    # 🔴 私有化让**运行时**的越界当场炸（NameError）；这条守卫扫**源码**，
    #    把"还没跑到"的那条路也堵上：谁把 `_ARTIFACTS` / `ARTIFACTS` 写回来，这里红。
    print("\n② 产物库只有一个入口：外部不许碰私有表")
    bad: list[str] = []
    for p in _py_files():
        if p.name == "artifacts.py":
            continue
        for n in ast.walk(_tree(p)):
            if isinstance(n, ast.Name) and n.id in ("_ARTIFACTS", "ARTIFACTS"):
                bad.append(f"{p.relative_to(ROOT)}:{n.lineno}（名字）")
            elif isinstance(n, ast.Attribute) and n.attr in ("_ARTIFACTS", "ARTIFACTS"):
                bad.append(f"{p.relative_to(ROOT)}:{n.lineno}（属性）")
            elif isinstance(n, ast.ImportFrom) and n.module == "proto.artifacts":
                bad += [
                    f"{p.relative_to(ROOT)}:{n.lineno}（import {a.name}）"
                    for a in n.names
                    if a.name in ("_ARTIFACTS", "ARTIFACTS")
                ]
    print(f"  {'✅' if not bad else '❌'} 除 `proto/artifacts.py` 外没人引用那张表" + (f"\n      {bad}" if bad else ""))
    if bad:
        raise SystemExit(1)

    # ── ③ 落盘 = 库里那份，**一字不加**（2026-11-06，外测 SX-13）──
    #
    # 平台原来给非 HTML 产物加"来源头"（`# {title}` ＋ id/字数 ＋ `---`）—— 那个 H1
    # **冒充了文档标题**：技能模板的正文首行是 `## 《课题》导学单`（H2），于是产物首部
    # 出现**两个同名标题**（实测：第 1 行 `# 《三角形的内角和》导学单`、第 7 行 `## 同`）。
    # 用户定：**正文就是正文，平台不必加料** —— 落盘字节 == 库里 content。
    # ⚠️ 有效性自检：给落盘加回任何前缀（哪怕一个换行），这里必须红。
    print("\n③ 落盘 = 库里那份，一字不加（'来源头'曾冒充文档标题）")
    artifacts.clear()
    with tempfile.TemporaryDirectory() as td:
        artifacts.use_dir(Path(td))
        bad_files: list[str] = []
        for ft, content in (("md", "## 《三角形的内角和》导学单\n\n正文"),
                            ("html", "<!DOCTYPE html><html>…</html>")):
            aid = store_artifact("《三角形的内角和》导学单", "导学单", ft, content)
            got = (Path(td) / artifacts.get(aid)["file"]).read_text(encoding="utf-8")
            if got != content:
                bad_files.append(f"{ft}：落盘 {got[:40]!r} != content {content[:40]!r}")
        artifacts.use_dir(None)   # 还原：不设 = 只进内存
    print(f"  {'✅' if not bad_files else '❌'} 落盘逐字等于库里的 content（不加头、不加尾）"
          + (f"\n      {bad_files}" if bad_files else ""))
    if bad_files:
        raise SystemExit(1)

    print(f"✅ 对照组炸了（{lost2} 次），说明测试**能**探到碰撞。")
    if lost:
        print(
            f"🔴 原样也撞了 {lost} 次 —— **发号器坏了，产物会静默丢失**。\n"
            "   查 `proto.artifacts._ART_SEQ`：它必须走 `itertools.count()`（`next()` 原子），\n"
            "   不能退回 `len(ARTIFACTS) + 1`（先查后写，两步之间有窗口）。"
        )
    else:
        print(
            "✅ 原样 **没撞上** —— 发号器结构上不重号（`itertools.count()`）。\n"
            "   这条现在守的是**回归**：谁把两步之间的活插回来、或退回 `len()`，这里就该红。"
        )


if __name__ == "__main__":
    main()
