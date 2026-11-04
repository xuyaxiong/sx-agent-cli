"""单元级：**流式日志不许把两次模型调用的话黏成一行**（不调 LLM）。

    uv run python -m evals.unit_stream_join

**为什么要有它**（2026-11-02 实测踩到）：模型调工具前会先说一句、工具跑完再接着说，
**两段之间没有换行符** —— 攒在同一个缓冲区里就被黏成一句。实测现象：

    好的，信息齐了，我来生成导学单已生成，以下是交付情况：
    └─ 调 delegate 前说的 ──────┘└─ 工具返回后说的 ──┘

中间隔着 **85 秒**（执行器在干活），日志上却是连着的一句话。

**判据**：同一条 AI 消息的流式 chunk 共享 `id`，换一次模型调用就换 `id`。

> 有效性自检：把 `stream_agent` 里那段 `cid != cur_id` 的落盘逻辑删掉，本测试必须红。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import io
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from langchain_core.messages import AIMessageChunk

from proto import trace

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail else ""))


def _chunk(text: str, mid: str) -> AIMessageChunk:
    c = AIMessageChunk(content=text)
    c.id = mid
    return c


class _FakeGraph:
    """只按剧本吐 chunk 的假图 —— 不碰模型、不碰网络。"""

    def __init__(self, script: list[tuple[str, str]]) -> None:
        self.script = script

    def stream(self, state, config, stream_mode=None):  # noqa: ARG002
        for text, mid in self.script:
            yield "messages", (_chunk(text, mid), {"langgraph_node": "model"})
        yield "values", {"messages": []}


def _run(script: list[tuple[str, str]]) -> str:
    """跑一遍 stream_agent，把日志文件的内容读回来。"""
    buf = io.StringIO()
    old_fh = trace._fh
    trace._fh = buf
    try:
        for _ in trace.stream_agent(_FakeGraph(script), {}, {}, "测试", role=trace.AGENT):
            pass
    finally:
        trace._fh = old_fh
    return buf.getvalue()


def main() -> None:
    print("① 换一次模型调用 → 两段话**不能**黏成一行")
    # 剧本照抄实测：调工具前一句（不带换行）+ 工具返回后一句
    log = _run([
        ("好的，信息齐了，我来生成导学单", "msg-A"),
        ("已生成，以下是交付情况：\n", "msg-B"),
    ])
    lines = [ln for ln in log.splitlines() if "⟩" in ln]
    joined = "".join(ln.split("⟩", 1)[1] for ln in lines)
    check("没有黏成「…我来生成导学单已生成…」", "导学单已生成" not in joined, repr(joined))
    check("调工具前那句**单独成行**",
          any("我来生成导学单" in ln and "已生成" not in ln for ln in lines), repr(lines))
    check("工具返回后那句也单独成行", any("已生成，以下是交付情况" in ln for ln in lines))

    print("\n② 同一条消息里跨 chunk 的句子，**不许**被切开")
    log = _run([
        ("这是一句被切成", "msg-C"),
        ("三段的话，它就该在", "msg-C"),
        ("同一行里。\n", "msg-C"),
    ])
    lines = [ln for ln in log.splitlines() if "⟩" in ln]
    check("一条消息 = 一行", len(lines) == 1, repr(lines))
    check("内容完整", "这是一句被切成三段的话，它就该在同一行里。" in (lines[0] if lines else ""))

    print("\n③ provider 不给 `id` 时，**保持原行为**（不要瞎猜边界）")
    log = _run([("前半句", None), ("后半句\n", None)])
    lines = [ln for ln in log.splitlines() if "⟩" in ln]
    check("没有 id 就不切（照旧攒成一行）",
          len(lines) == 1 and "前半句后半句" in lines[0], repr(lines))

    print("\n④ 基础设施抖动：**重试一次**；两次都失败才抛（2026-11-04 复审 N2/R8）")
    # 🔴 这是**两条链唯一共用的缝**（主 agent 的循环 / 执行器的循环都从这过），
    #    所以兜底兜在这里 —— 测的也是这一层：第一次抛、第二次成，整轮要照常跑完。
    class _FlakyGraph:
        """第一次 `stream` 直接抛（模拟抖动），第二次正常吐。"""

        def __init__(self) -> None:
            self.calls = 0

        def stream(self, state, config, stream_mode=None):  # noqa: ARG002
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("（模拟）sandbox grammar compile timed out")
            yield "messages", (_chunk("重试之后说的话\n", "m1"), {"langgraph_node": "model"})
            yield "values", {"messages": []}

    buf = io.StringIO()
    old_fh, trace._fh = trace._fh, buf
    g = _FlakyGraph()
    try:
        got = list(trace.stream_agent(g, {}, {}, "测试", role=trace.AGENT))
    finally:
        trace._fh = old_fh
    log = buf.getvalue()
    check("抖动一次后**重试成功**（整轮照常返回）", bool(got) and g.calls == 2, f"calls={g.calls}")
    check("重试后的话照常流出去", "重试之后说的话" in log, repr(log[-200:]))
    check("**说破了**：日志里有一行「重试一次」（不然读的人以为模型把话说重了）",
          "重试一次" in log and "基础设施抖动" in log, repr(log[-300:]))

    class _AlwaysFlaky:
        def stream(self, state, config, stream_mode=None):  # noqa: ARG002
            raise TimeoutError("（模拟）一直抖动")
            yield  # pragma: no cover —— 让它是生成器

    raised = False
    try:
        list(trace.stream_agent(_AlwaysFlaky(), {}, {}, "测试", role=trace.AGENT))
    except TimeoutError:
        raised = True
    check("两次都失败 → **原样抛出**（降级由调用方决定，这里不吞）", raised)

    print("\n⑤ 抖动发生在**吐过之后**：那半行必须留在日志里（不许静默丢）")
    # 🔴 2026-11-04 深度审计 §P1-4 实测：`buf` 是**生成器局部变量**，异常时不 flush →
    #    "被作废的那半行说了什么"在日志里**一个字都没有**，而 retry note 只说"那半段作废"。
    #    判据②**切了必须标** —— 标了"作废"却看不到作废的是什么，等于没标。
    #    ⚠️ ④ 只覆盖了"第一次**直接抛**"（还没动手）—— 那是最理想的抖动位置，测不到这条。

    class _FlakyAfterText:
        """第一次**吐了半行（无换行）**之后才抛；第二次正常吐。"""

        def __init__(self) -> None:
            self.calls = 0

        def stream(self, state, config, stream_mode=None):  # noqa: ARG002
            self.calls += 1
            yield "messages", (_chunk(f"这半行来自第{self.calls}次尝试", "m1"),
                               {"langgraph_node": "model"})
            if self.calls == 1:
                raise TimeoutError("（模拟）吐到一半抖动")
            yield "values", {"messages": []}

    buf = io.StringIO()
    old_fh, trace._fh = trace._fh, buf
    try:
        list(trace.stream_agent(_FlakyAfterText(), {}, {}, "测试", role=trace.AGENT))
    finally:
        trace._fh = old_fh
    log = buf.getvalue()
    check("第 1 次尝试那半行**在日志里**（不然读者不知道被作废的是什么）",
          "这半行来自第1次尝试" in log, repr(log[-300:]))
    check("第 2 次那半行也在（重试真的重跑了）", "这半行来自第2次尝试" in log, repr(log[-300:]))

    print("\n⑥ 抖动落在**第一步之后**：不再重试，原样抛（2026-11-05「做法二」）")
    # 🔴 **为什么画这条边界**：从头重跑 = `state` 干净、**副作用不干净** ——
    #    重试会把这一轮**已经做过的工具调用再做一遍**（`delegate` 重跑 = 执行器再跑
    #    60–120 秒 ＋ 再写一份产物；而模型在重试的 state 里看不到自己刚派过，很可能再派一次）。
    #    边界画在**第一个 super-step 走完**：`_stream_agent` 只在**每步结束时** yield
    #    （流式的字是发 `stream` 事件，不走 yield），所以"吐过东西"＝"至少一步走完了"。
    #    抖在那之前 = 什么都还没干 → 重试白赚（④ 守的就是这个）；之后 → 如实抛。
    #    ⚠️ 把这条判据拿掉（恢复无条件重试）→ 下面「只被调了 1 次」当场红。

    class _FlakyAfterStep:
        """吐完**一个 super-step** 之后才抛 —— 这时已经动过手了。"""

        def __init__(self) -> None:
            self.calls = 0

        def stream(self, state, config, stream_mode=None):  # noqa: ARG002
            self.calls += 1
            yield "values", {"messages": ["第一步走完了（比如 delegate 已经跑过）"]}
            raise TimeoutError("（模拟）第二步抖动")

    g = _FlakyAfterStep()
    raised, got = False, []
    try:
        for _c in trace.stream_agent(g, {}, {}, "测试", role=trace.AGENT):
            got.append(_c)
    except TimeoutError:
        raised = True
    check("第一步走完后再抖 → **原样抛**（不静默重做一遍）", raised, f"calls={g.calls}")
    check("确实**没有**再跑一次（`stream` 只被调了 1 次）", g.calls == 1, f"calls={g.calls}")
    check("第一步的产出**照常交出去**（不是吞掉）",
          bool(got) and "第一步走完了" in str(got[0]), repr(got))

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
