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

**端到端跑不到它。** 实测（`runs/baseline/e13-*`）：3 个并行 delegate 的写入时间戳
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
import asyncio
import io
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os

# 🔴 **单测的日志打 tag**（2026-11-06）：`logs/` 里真会话与测试垃圾混在一起，复盘时
#    grep 不动 —— 打上 tag 后文件名是 `proto-<ts>-unit-<名字>.log`，一眼可辨
#    （清理：`rm logs/*-unit-*.log`）。复用跑批那个 `PROTO_LOG_TAG`（`evals/_child.py`）。
# ⚠️ **必须排在所有非标准库 import 之前**：`proto.trace` 在 import 那一刻读 env 定文件名，
#    而 `from app import chat` 这种**间接** import 也会把它拉进来（实测踩过：
#    第一版插在 `from app import chat` 之后 → 日志照样没 tag）。
#    守卫：`unit_feed_waiting` ⑮。
os.environ["PROTO_LOG_TAG"] = "unit-artifact-race"
os.environ["PROTO_LOG_DIR"] = "tmp/logs"   # 🔴 测试日志是**噪音** —— 别混进给人看的 `logs/`

from proto import artifacts, session
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

    # ── ④ 会话隔离（2026-11-06）──
    #
    # 用户定：*"我这个会封装成一个库 面向多用户 所以所有东西都应该是面向 session 级的"*。
    # 原来表 / 发号器 / 落盘目录都是**模块级** —— 于是 `list_artifacts` 的 docstring 写着
    # "**本会话**"、读的却是**进程级**那张表：多会话时用户 B 会看到用户 A 的**全部产物**
    # （含落盘路径），ID 空间也共享。
    # ⚠️ **有效性自检**：把 `artifacts` 里任意一处 `session.current()` 改回模块级全局，
    #    下面必须红。
    print("\n④ 会话隔离：两个会话并行，A 看不见 B 的产物")
    artifacts.clear()
    with session.session(thread_id="user-A"):
        a_id = store_artifact("A 的教案", "A 的", "md", "A 的内容")
        a_list = asyncio.run(artifacts.list_artifacts.ainvoke({}))   # 真工具（`@tool` 包过）
    with session.session(thread_id="user-B"):
        b_id = store_artifact("B 的导学单", "B 的", "md", "B 的内容")
        b_list = asyncio.run(artifacts.list_artifacts.ainvoke({}))
        b_reads_a = artifacts.get(a_id)
        b_count = artifacts.count()

    leaks: list[str] = []
    if a_id != "art_001" or b_id != "art_001":
        leaks.append(f"发号器不是会话级的：A={a_id} B={b_id}（各自都该从 art_001 起）")
    if "A 的教案" in b_list:
        leaks.append(f"B 的清单里出现了 A 的产物：{b_list!r}")
    if "B 的导学单" in a_list:
        leaks.append(f"A 的清单里出现了 B 的产物：{a_list!r}")
    # 🔴 **这里断的不是"读不到"，是"读到的是自己的"**：两个会话**各从 `art_001` 起**
    #    ⇒ **跨会话重号是设计如此**。所以 B 拿 `art_001` 应当读到 **B 自己**那份；
    #    读到 A 的才是泄漏。（第一版我把断言写成 `is None`，它当场红了 ——
    #    **红得对，是断言错了**：重号不假，但隔离成立。）
    if b_reads_a is None or b_reads_a.get("title") != "B 的导学单":
        leaks.append(f"B 拿 A 的 ID `get()` 读到的不是自己那份：{b_reads_a!r}")
    if b_count != 1:
        leaks.append(f"B 的库里有 {b_count} 条（该只有它自己那 1 条）")
    print(f"  {'✅' if not leaks else '❌'} 两个会话的产物**互不可见**"
          + (f"\n      {leaks}" if leaks else ""))
    if leaks:
        raise SystemExit(1)
    # ⚠️ **重号把一件事从"锦上添花"变成"必需"**：多会话日志/事件里光看 `art_001`
    #    **指代不明** —— 所以"事件带 `thread_id`、日志行带会话列"不是可选项（那是下一轮）。
    print("  ✅ 跨会话**重号**（各从 art_001 起）—— 这是设计；⚠️ 因此日志/事件"
          "**必须带会话标识**才不歧义（下一轮做）")

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
