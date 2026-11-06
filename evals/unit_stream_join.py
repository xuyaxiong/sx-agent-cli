"""单元级：**流式日志不许把两次模型调用的话黏成一行**（不调 LLM）。

    uv run python -m evals.unit_stream_join

**为什么要有它**（2026-11-02 实测踩到）：模型调工具前会先说一句、工具跑完再接着说，
**两段之间没有换行符** —— 攒在同一个缓冲区里就被黏成一句。实测现象：

    好的，信息齐了，我来生成导学单已生成，以下是交付情况：
    └─ 调 delegate 前说的 ──────┘└─ 工具返回后说的 ──┘

中间隔着 **85 秒**（执行器在干活），日志上却是连着的一句话。

**判据**：同一条 AI 消息的流式 chunk 共享 `id`，换一次模型调用就换 `id`。

> 有效性自检：把 `stream_agent` 里那段 `cid != cur_id` 的落盘逻辑删掉，本测试必须红。

⚠️ 它还守着**重试边界**（④–⑦）：抖动落在哪能重试、落在哪如实抛。边界依赖两条
**框架前提**（"第一个 values chunk 是输入 state 本身"、"之后每步一个 chunk"）——
由 ⑦ 拿真 langgraph 图钉住（假图只是剧本，钉不住框架本身）。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import io
import sys
from typing import TypedDict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os

# 🔴 **单测的日志打 tag**（2026-11-06）：`logs/` 里真会话与测试垃圾混在一起，复盘时
#    grep 不动 —— 打上 tag 后文件名是 `proto-<ts>-unit-<名字>.log`，一眼可辨
#    （清理：`rm logs/*-unit-*.log`）。复用跑批那个 `PROTO_LOG_TAG`（`evals/_child.py`）。
# ⚠️ **必须排在所有非标准库 import 之前**：`proto.trace` 在 import 那一刻读 env 定文件名，
#    而 `from evals import chat` 这种**间接** import 也会把它拉进来（实测踩过：
#    第一版插在 `from evals import chat` 之后 → 日志照样没 tag）。
#    守卫：`unit_feed_waiting` ⑮。
os.environ["PROTO_LOG_TAG"] = "unit-stream-join"

from langchain_core.messages import AIMessageChunk
from langgraph.graph import END, START, StateGraph

from proto import session, trace

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
    _s = session.default()
    old_fh = _s.log_fh
    _s.log_fh = buf
    try:
        for _ in trace.stream_agent(_FakeGraph(script), {}, {}, "测试", role=trace.AGENT):
            pass
    finally:
        _s.log_fh = old_fh
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
    #
    # 🔴 **剧本 A 是真实形状**（2026-11-05 修 F1 时补）：真实图的第一个 values chunk
    #    是**输入 state 本身**（先于任何 super-step），抖动就发生在它之后的第一步里。
    #    原来只有剧本 B（"什么都没吐就抛"）—— 真实图**走不到那个形状**，于是守卫
    #    全绿而 bug 活着：判定用"吐过东西"，被输入 chunk 提前置真 → 第一步抖动从不重试。
    #    ⚠️ 剧本 A 在**修之前必须红**（`raised=True, calls=1`），修完才绿 —— 这才叫挡人。

    class _FlakyFirstStep:
        """真实形状：第一次**先吐输入 state chunk、再炸**（= 第一个 super-step 抖动）。"""

        def __init__(self) -> None:
            self.calls = 0

        def stream(self, state, config, stream_mode=None):  # noqa: ARG002
            self.calls += 1
            yield "values", state                      # 真实图开跑先吐这个（什么都没干）
            if self.calls == 1:
                raise TimeoutError("（模拟）第一个 super-step 抖动（第一次 LLM 调用）")
            yield "values", {"messages": ["重试之后第一步走完了"]}

    buf = io.StringIO()
    _s = session.default()
    old_fh, _s.log_fh = _s.log_fh, buf
    g = _FlakyFirstStep()
    raised, got = False, []
    try:
        for _c in trace.stream_agent(g, {"messages": []}, {}, "测试", role=trace.AGENT):
            got.append(_c)
    except TimeoutError:
        raised = True
    finally:
        _s.log_fh = old_fh
    log = buf.getvalue()
    check("第一步抖动（输入 chunk 已吐）→ **重试**，不原样抛",
          not raised and g.calls == 2, f"raised={raised}, calls={g.calls}")
    check("重试后整轮照常跑完（拿到第一步走完的 chunk）",
          any("重试之后第一步走完了" in str(c) for c in got), repr(got))
    check("**说破了**：日志里有一行「重试一次」（不然读的人以为模型把话说重了）",
          "重试一次" in log and "基础设施抖动" in log, repr(log[-300:]))

    class _FlakyAtStart:
        """第一次 `stream` 直接抛（**还没吐任何 chunk** —— 流还没建起来就失败），第二次正常吐。"""

        def __init__(self) -> None:
            self.calls = 0

        def stream(self, state, config, stream_mode=None):  # noqa: ARG002
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("（模拟）sandbox grammar compile timed out")
            yield "messages", (_chunk("重试之后说的话\n", "m1"), {"langgraph_node": "model"})
            yield "values", {"messages": []}

    buf = io.StringIO()
    _s = session.default()
    old_fh, _s.log_fh = _s.log_fh, buf
    g = _FlakyAtStart()
    try:
        got = list(trace.stream_agent(g, {}, {}, "测试", role=trace.AGENT))
    finally:
        _s.log_fh = old_fh
    log = buf.getvalue()
    check("抖在**任何 chunk 之前** → 重试成功（整轮照常返回）",
          bool(got) and g.calls == 2, f"calls={g.calls}")
    check("重试后的话照常流出去", "重试之后说的话" in log, repr(log[-200:]))

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

    # 🔴 **确定性拒绝（4xx，除 408/429）不重试**（2026-11-06，外测 SX-07）：那是上游对
    #    **这份内容**的拒绝 —— 重发同一份会再被拒（大生成很贵，白烧一次）。
    #    分类见 `trace.upstream_rejected`；"换措辞而不是原样再派"的说法由
    #    `fault_kind` 那条链管（`unit_receipt` / `unit_handoff`）。
    #    ⚠️ 有效性自检：把 `_stream_with_retry` 里 `upstream_rejected(e)` 那个条件删掉，
    #    下面「不重试」那条必须红。
    class _Http400(Exception):
        status_code = 400

    class _Rejected:
        def __init__(self) -> None:
            self.calls = 0

        def stream(self, state, config, stream_mode=None):  # noqa: ARG002
            self.calls += 1
            raise _Http400("Output data may contain inappropriate content.")
            yield  # pragma: no cover —— 让它是生成器

    g = _Rejected()
    raised = False
    try:
        list(trace.stream_agent(g, {}, {}, "测试", role=trace.AGENT))
    except _Http400:
        raised = True
    check("4xx 拒绝：**原样抛**（降级由调用方决定）", raised)
    check("4xx 拒绝：**不重试**（`stream` 只被调 1 次 —— 重发同一份内容没意义）",
          g.calls == 1, f"calls={g.calls}")

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
    _s = session.default()
    old_fh, _s.log_fh = _s.log_fh, buf
    try:
        list(trace.stream_agent(_FlakyAfterText(), {}, {}, "测试", role=trace.AGENT))
    finally:
        _s.log_fh = old_fh
    log = buf.getvalue()
    check("第 1 次尝试那半行**在日志里**（不然读者不知道被作废的是什么）",
          "这半行来自第1次尝试" in log, repr(log[-300:]))
    check("第 2 次那半行也在（重试真的重跑了）", "这半行来自第2次尝试" in log, repr(log[-300:]))

    print("\n⑥ 抖动落在**第一步之后**：不再重试，原样抛（2026-11-05「做法二」）")
    # 🔴 **为什么画这条边界**：从头重跑 = `state` 干净、**副作用不干净** ——
    #    重试会把这一轮**已经做过的工具调用再做一遍**（`delegate` 重跑 = 执行器再跑
    #    60–120 秒 ＋ 再写一份产物；而模型在重试的 state 里看不到自己刚派过，很可能再派一次）。
    #    边界画在**第一个 super-step 走完** —— ⚠️ 判据是**数到第二个 values chunk**
    #    （第一个是输入 state 本身，见 ⑦），**不是**"吐过东西"。
    #    抖在第一步里 = 什么都还没干 → 重试白赚（④ 剧本 A 守的就是这个）；之后 → 如实抛。
    #    ⚠️ 把这条判据拿掉（恢复无条件重试）→ 下面「只被调了 1 次」当场红。

    class _FlakyAfterStep:
        """真实形状：**先吐输入 state**、**再吐第一步走完**，然后才抛 —— 这时已经动过手了。"""

        def __init__(self) -> None:
            self.calls = 0

        def stream(self, state, config, stream_mode=None):  # noqa: ARG002
            self.calls += 1
            yield "values", state
            yield "values", {"messages": ["第一步走完了（比如 delegate 已经跑过）"]}
            raise TimeoutError("（模拟）第二步抖动")

    g = _FlakyAfterStep()
    raised, got = False, []
    try:
        for _c in trace.stream_agent(g, {"messages": []}, {}, "测试", role=trace.AGENT):
            got.append(_c)
    except TimeoutError:
        raised = True
    check("第一步走完后再抖 → **原样抛**（不静默重做一遍）", raised, f"calls={g.calls}")
    check("确实**没有**再跑一次（`stream` 只被调了 1 次）", g.calls == 1, f"calls={g.calls}")
    check("第一步的产出**照常交出去**（不是吞掉）",
          len(got) == 2 and "第一步走完了" in str(got[1]), repr(got))

    print("\n⑦ 真实图的 chunk 形状 —— 上面那条边界**依赖这两条前提**，钉住它们（不调 LLM）")
    # 🔴 边界判定（"数到第二个 values chunk"）建立在这两条**框架行为**上：
    #    ① 第一个 values chunk 是**输入 state 本身**，先于任何 super-step 执行；
    #    ② **只有真的改了 state 的步才吐 chunk**（没改的步不吐）。
    #    合起来：数到第二个 chunk ⇔ **至少一步动过 state** —— 这正是边界要的语义
    #    （"动过手"＝state 变过；我们的图里动过手必改 state：模型步加 AI 消息、
    #      工具步加 ToolMessage）。
    #    将来 langgraph 升级若改了任一条，边界会**静默漂移** —— 所以拿真图钉死
    #    （④⑥ 的假图只是剧本，钉不住框架本身）。

    class _S(TypedDict):
        x: int

    def _boom(state):  # noqa: ARG001
        raise TimeoutError("（模拟）首个节点抖动")

    g1 = StateGraph(_S)
    g1.add_node("boom", _boom)
    g1.add_edge(START, "boom")
    g1.add_edge("boom", END)
    seen: list = []
    raised = False
    try:
        for mode, payload in g1.compile().stream({"x": 1}, {}, stream_mode=["values", "messages"]):
            if mode == "values":
                seen.append(payload)
    except TimeoutError:
        raised = True
    check("① 首个节点炸之前，**输入 state 已经吐出来**（重试边界的全部依据）",
          raised and seen == [{"x": 1}], f"raised={raised}, seen={seen}")

    def _noop(state):  # noqa: ARG001
        return {}

    def _bump(state):
        return {"x": state["x"] + 1}

    g2 = StateGraph(_S)
    g2.add_node("a", _noop)
    g2.add_node("b", _bump)
    g2.add_edge(START, "a")
    g2.add_edge("a", "b")
    g2.add_edge("b", END)
    chunks2 = [p for mode, p in g2.compile().stream({"x": 1}, {}, stream_mode=["values", "messages"])
               if mode == "values"]
    check("② 改了 state 的步才出 chunk（没改的不出）→ 数 chunk ＝ 数'动过手的步'",
          chunks2 == [{"x": 1}, {"x": 2}], f"chunks={chunks2}")

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
