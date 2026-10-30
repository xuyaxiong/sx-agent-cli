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

import sys
import threading
import time
from collections import Counter

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from proto.artifacts import ARTIFACTS, store_artifact

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
    ARTIFACTS.clear()
    n, lost = hammer(store_artifact, "原样")
    print(f"【原样】     写入 {n}｜实存 {len(ARTIFACTS)}｜**撞掉 {lost}**")

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

    print(f"✅ 对照组炸了（{lost2} 次），说明测试**能**探到碰撞。")
    if lost:
        print(
            f"🔴 原样也撞了 {lost} 次 —— **发号器坏了，产物会静默丢失**。\n"
            "   查 `proto.skills._ART_SEQ`：它必须走 `itertools.count()`（`next()` 原子），\n"
            "   不能退回 `len(ARTIFACTS) + 1`（先查后写，两步之间有窗口）。"
        )
    else:
        print(
            "✅ 原样 **没撞上** —— 发号器结构上不重号（`itertools.count()`）。\n"
            "   这条现在守的是**回归**：谁把两步之间的活插回来、或退回 `len()`，这里就该红。"
        )


if __name__ == "__main__":
    main()
