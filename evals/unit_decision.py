"""单元级：**决策记录**（不调 LLM）—— 一次**判断** = 一条记录，四段齐全。

⚠️ 2026-11-05 起"判断"有两种（**同一套记录形状**，见 `handoff.decision` 的模块 docstring）：
**LLM 决策**（试答，是交接）／**平台的结构判定**（「任务完成没」，不是交接）。

    uv run python -m evals.unit_decision

用户 2026-11-03：*"decide 那一段日志也得整理下格式，人看着也不清晰。既然是决策，
很显然：**决策前提是什么、要决策的问题是什么、决策结果是什么**，有可能还要补充
**下一步该干什么** —— 你现在的打印也是人类不好看懂。"*

⚠️ **这个测试管不了"模型读懂没有"** —— 那要真跑（`uv run python -m evals.chat`）。
它管的是**机制**，就两件事：

**① 记录层**：`trace.decision` 渲出来的四段**齐全、顺序对、值一个字不丢**，
   而且列表**不是 Python repr**（原来是 `['…', '…']`，括号引号全是噪音）。

**② 记录层 ↔ 展示层不许失配**：`evals/chat.py` 认决策块走**结构化事件**（`trace.decision`
   发的 `{"type": "decision", …}`，四段在字段里）—— 展示层**不再解析渲染文本**。
   旧契约（从 `trace.DECISION_HEAD` 前缀认块）是**文本契约**：措辞一改就**静默失配**，
   块认不出来 → 整块当噪音丢掉 —— 而丢的正是"平台为什么这么决定"。
   同 `handoff.SECTION` 那条教训：harness 手抄段名，回执改了格式之后
   **新格式 0/8 命中**，12560 字的回执被静默切成 1200。

**⑤ 段名只有一份**：`trace.DECISION` 加一段 → 记录层与展示层**都得跟着走**
   （两边各抄一份的后果实测过：记录层整块决策从日志里消失、展示层静默少一段）。

**⑥ 发射器不漏键**：`trace.decision` 发的事件必须覆盖 `DECISION` 的每一个键。

> 有效性自检：把 `trace.decision` 改回只发渲染文本（不发字段），③ 必须红。
> ⑤ 把 `chat._render_decision` 改回写死那四段（或 `_recorder` 的 `vals` 写死四键），⑤ 必须红。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import io
import sys

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
os.environ["PROTO_LOG_TAG"] = "unit-decision"

from evals import chat

from proto import trace

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail and not ok else ""))


def _capture(fn) -> str:
    """跑一段记录，把它**新写进日志的那几行**捞出来（不碰别的输出）。"""
    before = trace.LOG_FILE.stat().st_size
    fn()
    with io.open(trace.LOG_FILE, "rb") as f:
        f.seek(before)
        return f.read().decode("utf-8", "replace")


#: 一段**真实形状**的判定结果（含多行值、含列表）—— 空值测不出"漏渲染"。
def _sample() -> None:
    trace.decision(
        "判定（LLM）· R02 第 1 轮",
        q="这个任务完成了没？",
        given=[
            "技能 lesson-prep（instructions 2532 字，按名引用）",
            "材料 执行器说的话（无产物）",
        ],
        result=[
            "status = **waiting_for_input**",
            "理由：任务要求生成完整教案，但第二个问题还没收到答复。",
            "还缺：\n- 尚未收到用户对第二个问题的答复\n- 教案文本文件产物为空",
        ],
        nxt="登记待答 → 平台强制主 agent 试答 → 答不上就**原样透传**给用户",
    )


def main() -> None:
    got = _capture(_sample)
    print("① 四段齐全、顺序 = 用户定的那个顺序（前提 → 问题 → 结果 → 下一步）")
    from proto.handoff import HOPS, SECTION
    _secs = [SECTION[k] for k in HOPS["decision"]]
    heads = [f"{trace._DECISION_HEAD} · ", *[f"    {v}" for v in _secs]]
    for h in heads:
        check(f"`{h.strip()}` 在", h in got, repr(got[:120]))
    pos = [got.find(f"    {v}") for v in _secs]
    check("四段的**先后顺序**对", pos == sorted(pos) and all(p >= 0 for p in pos), f"位置={pos}")
    check("表里的段名**都用到了**（没有死段）",
          all(f"{v}" in got for v in _secs))

    print("\n② 值**一个字都不丢**，且列表**不是 Python repr**")
    for frag in ("技能 lesson-prep（instructions 2532 字，按名引用）",
                 "status = **waiting_for_input**",
                 "理由：任务要求生成完整教案，但第二个问题还没收到答复。",
                 "尚未收到用户对第二个问题的答复",
                 "教案文本文件产物为空",
                 "登记待答 → 平台强制主 agent 试答 → 答不上就**原样透传**给用户"):
        check(f"`{frag[:22]}…` 原样在", frag in got)
    check("列表**逐条成行**（`- ` 起头），不是 `['…', '…']` 那种 repr",
          "      - 尚未收到用户对第二个问题的答复" in got and "['" not in got)

    print("\n③ 展示层认决策块走**结构化事件** —— 不许从文本反解析")
    src = io.open("evals/chat.py", encoding="utf-8").read()
    # 旧契约（`body.startswith(trace.DECISION_HEAD)`）已废：展示层订阅 `decision`
    # 事件、从**字段**自取四段 —— 措辞怎么改都不可能失配。
    check("`chat.py` 订阅了事件总线", "trace.subscribe(" in src)
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    for gone in ("DECISION_HEAD", "轮判定：status=", "判定输入："):
        check(f"`chat.py` 的**代码**里不再出现 `{gone}`", gone not in code)
    # 🔴 记录层的**落盘格式**（`_DECISION_HEAD` / `_STREAM_MARK`）也不许伸手（2026-11-04 复审 N1）：
    # 名字私有之后，写出来就是"我在读记录层的产物"的信号 —— 这条把它变成**会红**的规矩。
    # ⚠️ `brief_args` **不在**这张名单里：它是**两层共用**的渲染助手（展示层也要把工具参数
    #    渲染成同一副面孔），公开是对的 —— 名单只收"记录层自己的格式"。
    for gone in ("_DECISION_HEAD", "_STREAM_MARK", "render_message"):
        check(f"`chat.py` 的**代码**里不出现记录层的 `{gone}`", gone not in code)
    check("`trace.decision` 发的是结构化决策事件（字段原样在 ev 里）",
          '"type": "decision"' in io.open("proto/trace.py", encoding="utf-8").read())

    print("\n④ 判断都走 `trace.decision`（不再是散装 note）")
    # ⚠️ 2026-11-05 判定退休：**判定那次 LLM 调用整个没了**（三态改由平台按结构定）。
    #    现在系统里有两处判断，**都走这一条记录形状**：
    #      ① **试答**（LLM，`agent.py`）—— 它是交接；
    #      ② **「任务完成没」**（平台按结构定，`executor.py`）—— **不是交接**（没有 LLM 参与），
    #         但用户点名要审计它（*"给它的上下文是啥、结果又是啥、产生结果依据又是啥"*）——
    #         记录形状照旧，另起一套 = 同一个问题两副面孔。
    s = io.open("proto/agent.py", encoding="utf-8").read()
    check("`agent.py` 调了 `trace.decision(`（试答）", "trace.decision(" in s)
    ex = io.open("proto/executor.py", encoding="utf-8").read()
    check("`executor.py` 里**没有 LLM 判定**（判定退休）—— 没有 `safe_structured(`",
          "safe_structured(" not in ex)
    check("`executor.py` 的「任务完成没」**有记录**（走同一条路，不是散装 note）",
          "trace.decision(**_verdict(" in ex,
          "少了它 → 「完成没」这个判断在日志里又没有上下文/依据了")

    print("\n⑤ 段名**只有一份**：给 `_DECISION_KEYS` 加一段，两层都得跟着走")
    # 🔴 原来两边**各抄一份四段表**（记录器的 `vals` 一份、展示层的 `(标签, 键)` 一份）。
    # 加一段的后果（2026-11-03 审核实测）：记录层**整块决策从日志里消失**
    # （KeyError 被总线的兜底吃掉，只剩一行 ⚠️）、展示层**静默少一段**。
    #
    # ⚠️ **2026-11-05**：段名表从 `trace.DECISION` 搬到了 `SECTION` ＋
    # `HOPS["decision"]` —— 那份是**第二份段名表**（不带 `【】`、缩进另算），
    # 把"段名一律 `【…】` 独占行首"那条硬规矩**开了第二个口子** ✗。
    from proto.handoff import SECTION
    from proto.handoff.decision import _DECISION_KEYS

    # ⚠️ **加一段要动三处** —— 它们是**三件不同的事**，不是"同一份抄三遍"：
    #    ① `SECTION`：段名本身（**所有段名的家**）
    #    ② `HOPS["decision"]`：**这一跳**用哪几段、什么顺序（`render_hop` 照着走）
    #    ③ `_DECISION_KEYS`：**事件字段 → 段名 key** 的映射（事件里叫 `cost`）
    from proto.handoff import HOPS
    _DECISION_KEYS["cost"] = "dec_cost"
    SECTION["dec_cost"] = "【成本】"
    HOPS["decision"] = (*HOPS["decision"], "dec_cost")
    try:
        ev5 = {"type": "decision", "kind": "", "who": "R02 第 1 轮判定", "q": "做完了吗",
               "given": "材料 5136 字", "result": "incomplete", "next": "推它继续",
               "cost": "6.2s", "ts": "2026-11-03 12:00:00.000"}
        got5 = _capture(lambda: trace._publish(ev5))
        check("记录层渲染出了新段（不是 KeyError 丢整块）", "【成本】" in got5, repr(got5[:200]))
        seen5: list[str] = []
        real_emit = chat._emit
        chat._emit = lambda role, text, stamp=None, *, cap=False: seen5.append(text)  # type: ignore[assignment]
        try:
            chat._on_event(ev5)
        finally:
            chat._emit = real_emit  # type: ignore[assignment]
        check("展示层也渲染出了新段（不静默少一段）",
              bool(seen5) and "【成本】" in seen5[-1], repr(seen5[-1][:200]) if seen5 else "（没播）")
    finally:
        del _DECISION_KEYS["cost"]
        del SECTION["dec_cost"]
        HOPS["decision"] = tuple(k for k in HOPS["decision"] if k != "dec_cost")

    print("\n⑥ 发射器把 `DECISION` 的**每一个键**都放进事件（漏一个 = 静默丢一段）")
    evs: list[dict] = []
    trace.subscribe(evs.append)
    trace.decision("发射器检查", q="q", given="g", result="r", nxt="n")
    _fields = set(_DECISION_KEYS)
    check("事件里的键**覆盖** `_DECISION_KEYS`", bool(evs) and _fields <= set(evs[-1]),
          f"事件键={sorted(evs[-1]) if evs else '（没发）'}，需要={sorted(_fields)}")


    print("\n⑨ `safe_structured`：抖动 → 重试一次 → **保守默认**（不炸整轮）")
    # 🔴 2026-11-05 从 `e11-fault-injection` **搬进来的**：那条 case 注入的故障打在
    #    "试答"的结构化调用上，**只有"执行器主动提问"才会走到那一步** ✗ —— 模型不问
    #    就没处可打（`fault_fired` 实测 7 次里 1 次报"触发 0 次"）。**时红时绿的测试
    #    就是噪音**，所以把覆盖搬到这里：确定、不调 LLM、秒级 ✓。
    #    （流的抖动由 `e12-fault-stream` 覆盖 —— 那个注入点**必然**发生 ✓）
    from proto import llm as llm_mod
    from proto.llm import safe_structured

    class _Schema:
        pass

    class _Boom:
        calls = 0

        def with_structured_output(self, schema):  # noqa: ANN001
            return self

        def invoke(self, prompt):  # noqa: ANN001
            _Boom.calls += 1
            raise TimeoutError("（模拟）grammar 编译超时")

    class _Empty:
        def with_structured_output(self, schema):  # noqa: ANN001
            return self

        def invoke(self, prompt):  # noqa: ANN001
            return None                     # 模型吐了空（不抛）

    _orig = llm_mod.get_model
    try:
        llm_mod.get_model = lambda: _Boom()          # type: ignore[assignment]
        got = safe_structured(_Schema, "p", "保守默认", tag="测试",
                              log_prompt=False, log_response=False)
        llm_mod.get_model = lambda: _Empty()         # type: ignore[assignment]
        got2 = safe_structured(_Schema, "p", "保守默认", tag="测试",
                               log_prompt=False, log_response=False)
    finally:
        llm_mod.get_model = _orig                    # type: ignore[assignment]
    check("两次都失败 → 返回**保守默认值**（不抛、不炸整轮）", got == "保守默认", repr(got))
    check("**重试了一次**（模型被调 2 次）", _Boom.calls == 2, f"{_Boom.calls} 次")
    check("**返回 `None` 也算失败** → 走默认值（不把 None 交给调用方）",
          got2 == "保守默认", repr(got2))

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
