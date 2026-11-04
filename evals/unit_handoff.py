"""单元级：**交接三问**（不调 LLM）。

    uv run python -m evals.unit_handoff

用户 2026-11-02 定的规矩，原话：

> "一定要注意交接的地方：**① 交接了什么内容　② 内容是否被截断了
>  ③ 过长的交接内容是否考虑了渐进式披露的原则** —— 这三点深刻影响 agent 执行效果。"

这个测试把它变成机制（不再是"记得就好"）：

**① 源码扫描** —— `proto/*.py` 里每一处切片（`[:N]` / `[-N:]`）**要么在豁免名单里写明理由，
要么就不许有**。以后谁再手滑加一个 `report[:150]`，这里当场红。
（和 `unit_briefing.py` 的 `EXCLUDED` 同构：**加字段只有两条路，渲染它，或者写明为什么不**。）

**② 逐交接点用超长输入验完整性** —— 每个交接渲染器喂一段"顶穿任何阈值"的输入，
断内容**一个字都没丢**；确实需要截的（台账那种每轮重复注入的索引），断它**标了**。

> 有效性自检：把 `render_briefing` 里 `report` 换成 `report[:200]`，② 必须红。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import io
import re
import sys
from glob import glob

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail and not ok else ""))


# ═══════════════════════════ ① 源码扫描 ═══════════════════════════

#: 切片形状：`[:N]`（切头）、`[-N:]`（切尾）。**不**包含 `[N:M]` / `[N:]`（那是取段、不是截断）。
SLICE = re.compile(r'\[\s*:\s*\d+\s*\]|\[\s*-\s*\d+\s*:\s*\]')

#: **豁免名单** —— 每一条都要有理由。键 = 文件名，值 = [(正则, 理由)]。
ALLOWED: dict[str, list[tuple[str, str]]] = {
    'proto/agent.py': [
        (r'tool_call_id', 'ID 派生（取尾部 8 位）—— 不是交接内容'),
    ],
    # ⚠️ **`proto/handoff/` 是包，`glob('proto/*.py')` 扫不到它** ——
    # 2026-11-03 把交接的 schema 与模板搬进包里时差点让它掉出守卫的覆盖面。
    #
    # ⚠️ `proto/handoff/briefing.py` 那条豁免（`q_lines[0]|q_lines[1:]`，"按行拆分渲染"）
    # **2026-11-03 已删** —— 那段换成了 `proto.handoff.body_lines`，豁免的正则再也匹配不到，
    # 本文件的 ALLOWED 自检当场报红（"代码改了就把这条豁免删掉，别留着烂"）。
    # 现在的多行拆分在 `body_lines` 里，不含切片形状，**不需要豁免**。
    'proto/handoff/receipt.py': [
        (r'原来七个返回口各拼各的字符串', '文档字符串里**引用**旧写法说明为什么改 —— 不是真代码'),
    ],
    'proto/handoff/ledger.py': [
        (r'text\[:limit\]', '**显式截断**：带【平台截断…】标记并指向回执 —— 见 `brief`'),
    ],
    'proto/trace.py': [
        (r'%f"\)\[:-3\]', '时间戳砍到毫秒'),
    ],
    'proto/config.py': [
        (r'parents\[1\]', '路径层级，不是文本'),
    ],
    'proto/skill_pack.py': [
        (r'lines\[i \+ 1 :\]|lines\[0\]', 'frontmatter 切分'),
    ],
    'proto/__main__.py': [
        (r'\[seen\[0\] :\]|\[0\]', '增量打印的游标'),
    ],
}


def scan_source() -> None:
    print("① 源码扫描：交接点上的切片，要么豁免、要么不许有")
    unlisted: list[str] = []
    # ⚠️ **两个 glob 都要**：`proto/handoff/` 是包，`proto/*.py` 扫不到它里面的模块。
    for f in sorted(glob('proto/*.py') + glob('proto/handoff/*.py')):
        key = f.replace(chr(92), '/')
        lines = io.open(f, encoding='utf-8').read().splitlines()
        for n, line in enumerate(lines, 1):
            if line.strip().startswith('#'):
                continue
            if not SLICE.search(line):
                continue
            if any(re.search(rx, line) for rx, _ in ALLOWED.get(key, [])):
                continue
            unlisted.append(f'{key}:{n}  {line.strip()[:100]}')
    check(
        "没有「未申报」的截断",
        not unlisted,
        "以下切片没有豁免理由 —— 交接内容不许悄悄切一刀；\n"
        "确实要切，就在本文件的 ALLOWED 里写明**为什么**（并确保切了要标）：\n  "
        + "\n  ".join(unlisted),
    )
    for key, items in ALLOWED.items():
        src = io.open(key, encoding='utf-8').read()
        for rx, why in items:
            check(f"豁免仍然成立：{key} · {why[:28]}", bool(re.search(rx, src)),
                  f"规则 `{rx}` 已经匹配不到了 —— 代码改了就把这条豁免删掉，别留着烂")


# ═══════════════════════════ ② 逐交接点验完整性 ═══════════════════════════

#: 顶穿任何阈值的超长文本（尾标记用来抓截断）
LONG = ("开头标记：这段交代写得很长。" + "内容。" * 400 + "　结尾标记：截断的话它一定不在。")


def main() -> None:
    scan_source()

    from langchain_core.messages import AIMessage, HumanMessage

    from proto.handoff.ask import build_prompt as _ask_prompt
    from proto.handoff.briefing import render_briefing
    from proto.handoff.receipt import build_receipt, render_receipt
    from proto import artifacts
    from proto.artifacts import store_artifact
    from proto.executor import ExecOutcome
    from proto.handoff.judgement import _Judgement
    from proto.handoff.nudge import nudge_text as _nudge_text
    from proto.graph import node_prepare

    print("\n② 逐个交接点：喂超长输入，断**一个字都没丢**")

    # ── ④ 交接书（平台 → 执行器）──
    artifacts.clear()
    aid = store_artifact("超长产物", "梗概" * 40, "md", "正文" * 900)
    # ⚠️ 用户原话用**另一个**长串：`qa` 的「答」是**全文**渲染的（那是执行器自己问来的），
    # 若两处共用 `LONG`，"正文没进上下文"这条断言会被 qa 那一份顶成**假绿**。
    TURN = "这一轮用户说了很长的一段话：" + "原话细节。" * 100
    task = {
        "id": "t1", "skill": "s", "task": "超长任务描述：" + "细节。" * 200,
        "stage": "waiting_for_input", "artifacts": [aid], "refs": [aid],
        "report": LONG, "progress": LONG, "missing": [LONG],
        "qa": [{"by": "用户", "q": LONG, "a": LONG}],
        "user_turns": [{"n": 1, "text": TURN, "after": LONG}],
    }
    brief = render_briefing(task)
    check("④ 交接书 · 任务全文在", task["task"] in brief)
    check("④ 交接书 · 执行器上次说的全文在", LONG in brief)
    check("④ 交接书 · 判定给的进度全文在", task["progress"] in brief)
    check("④ 交接书 · 缺什么全文在", task["missing"][0] in brief)
    # 🔴 **用户原话走的是「清单 + 按需」**（不是"不截"）：索引行在、正文按需 ——
    # 见 `briefing.turn_lines` 的分层。超长输入下**常驻层不许跟着涨**。
    check("④ 交接书 · 用户原话**索引行**在（序号 + 字数 + 开头）",
          f"第 1 轮（{len(TURN)} 字）" in brief and " ".join(TURN.split())[:40] in brief)
    check("④ 交接书 · 超一行的原话**正文没进上下文**（按需 `read_user_turn`）",
          TURN not in brief and "read_user_turn" in brief)
    check("④ 交接书 · 「它在回答的上一句」索引行在", "它在回答的上一句" in brief)
    check("④ 交接书 · 问答对全文在", task["qa"][0]["a"] in brief)

    # ── ⑧ 推回去（平台 → 执行器）──
    nudge = _nudge_text(_Judgement(status="incomplete", progress=LONG, missing=[LONG], reason=""))
    check("⑧ 推回去 · 进度全文在", LONG in nudge)
    check("⑧ 推回去 · 缺失项全文在", nudge.count(LONG) >= 2)

    # ── ⑨ 试答（平台 → 主 agent）──
    outcome = ExecOutcome(status="waiting_for_input", artifacts=[], report=LONG, rounds=1,
                          progress=LONG, missing=[LONG])
    ask = _ask_prompt("s", "超长任务：" + "细节。" * 200, outcome,
                      [HumanMessage(LONG), AIMessage(LONG)])
    check("⑨ 试答 · 执行器问的全文在", LONG in ask)
    check("⑨ 试答 · 主 agent 上下文里那条长消息**全文在**", LONG in ask)
    check("⑨ 试答 · 主 agent 上下文里那条长回复**全文在**", ask.count(LONG) >= 2)
    check("⑨ 试答 · 没有「最近 N 条」这种无声范围限制", "最近" not in ask.split("你的上下文")[1][:40])

    # ── ⑩ 回执（执行器 → 主 agent）—— 完整断言在 `unit_receipt.py`，这里只做交叉 ──
    receipt = render_receipt(build_receipt(outcome))
    check("⑩ 回执 · 执行器说的全文在（详见 unit_receipt.py）", LONG in receipt)

    # ── ② 台账注入（平台 → 主 agent）—— **每轮重复注入的索引，允许短，但必须标 ──
    artifacts.clear()
    st = {
        "messages": [AIMessage("（没有 HumanMessage，免得触发投递）")],
        "tasks": [{"id": "t1", "skill": "s", "stage": "waiting_for_input",
                   "artifacts": [], "refs": [], "report": LONG,
                   "progress": LONG, "missing": [LONG], "qa": [], "user_turns": []}],
    }
    injected = node_prepare(st)["messages"][0].content
    check("② 台账 · 长内容被截了（它是每轮重复注入的索引，允许短）", LONG not in injected)
    check("② 台账 · **但截了必须说**：带了平台截断标记", "【平台截断" in injected)
    # 🔴 **E2（2026-11-04 复审）："全文在哪"两条路不一样** —— `delegate` 那趟的全文在
    #    **回执**里（主 agent 自己的工具返回，就在它上下文里）；`feed`（用户答复后
    #    平台投递重跑）那趟**没有回执**，全文压根不在它上下文里。
    #    原来两条路都写"全文见那次的回执"—— 投递那条路指过去，只会翻到**上一轮**的回执
    #    （内容还是旧的），把旧的当成当前的：**指向"存在但内容不对"的出处比指不到更坏**。
    check("② 台账 · 说明了还差多少字", "还有" in injected and "字" in injected)
    check("② 台账 · `delegate` 那轮：指向回执",
          "全文见那次的回执" in
          node_prepare({**st, "tasks": [{**st["tasks"][0], "run_by": "delegate"}]})["messages"][0].content)
    check("② 台账 · `feed`／没记 run_by 时：**不许**指向回执，要说清「你手上没有」",
          "回执" not in injected and "不在你的上下文里" in injected)
    short_task = {**st["tasks"][0], "report": "很短", "progress": "也很短", "missing": ["短"]}
    check("② 台账 · 短内容**不该**被标截断",
          "【平台截断" not in node_prepare({**st, "tasks": [short_task]})["messages"][0].content)

    # ── ⑪ 透传（平台 → 用户）：注入的 AIMessage 里装的是**执行器的原话**，
    #      必须**打标记**，否则记录层会把它标成"主 agent 输出"（2026-11-03 实测踩到）
    from proto.graph import node_finalize

    fin = node_finalize({**st, "tasks": [short_task]})["messages"][0]
    tag = (fin.additional_kwargs or {}).get("injected_by")
    check("⑪ 透传 · 注入了 AIMessage", type(fin).__name__ == "AIMessage")
    check("⑪ 透传 · 打了 `injected_by` 标记", tag == "platform.finalize", f"tag={tag!r}")
    from proto.handoff import INJECTED

    check("⑪ 透传 · 标记在词汇表里声明过", tag in INJECTED, f"未声明：{tag!r}")
    from evals.harness import _injected_label

    check("⑪ 透传 · 记录层标的是**平台透传**（不是主 agent 输出）",
          "透传" in _injected_label(tag) and "不是主 agent" in _injected_label(tag),
          _injected_label(tag))
    check("⑪ 透传 · 没标记的消息**不**被误标", _injected_label(None) == "")

    # ── ⑭ 平台故障那条链：`run_task` **兜住**（不是炸整轮）→ 回执说清「原样再派」──
    # 🔴 2026-11-04 复审 N2/R8：`stream_agent` 重试过仍失败 → 原来异常一路穿出去炸整轮
    #    （用户白等 3 分钟 + 一屏栈）。现在 `run_task` 接住、记成 `fault`，交回主 agent 决定。
    # ⚠️ 打桩 `get_model` / `create_agent` / `stream_agent` —— **不碰真模型、不联网、秒级**。
    print("\n⑭ 平台故障：`run_task` 兜住（不是炸整轮），回执说清「原样再派」")
    import proto.executor as _ex
    from proto import trace
    from proto.handoff.receipt import build_receipt as _build

    _get_model, _create_agent, _stream = _ex.get_model, _ex.create_agent, trace.stream_agent
    _ex.get_model = lambda temperature=0.0: object()  # type: ignore[assignment]
    _ex.create_agent = lambda *a, **k: object()  # type: ignore[assignment]

    def _boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise TimeoutError("（模拟）sandbox grammar compile timed out")

    trace.stream_agent = _boom  # type: ignore[assignment]
    try:
        from proto.skills import Skill as _Skill

        out = _ex.run_task(_Skill(name="quiz", description="随堂小测", instructions="出 3 道题"),
                           "给三年级出 3 道随堂小测")
    finally:
        _ex.get_model, _ex.create_agent = _get_model, _create_agent  # type: ignore[assignment]
        trace.stream_agent = _stream  # type: ignore[assignment]

    check("异常**没有穿出去**（run_task 正常返回了结果）", out is not None)
    check("`status` 仍是三态里的 `incomplete`（**三态封闭，没有第四种**）",
          out.status == "incomplete", out.status)
    check("`fault` 记下了原因（复盘要看）", "TimeoutError" in out.fault, repr(out.fault))
    check("**没有编造 `report`**（它一个字都没说）", out.report == "", repr(out.report))
    check("回执的出口分类是 `fault`（不借 failed 的文案）",
          _build(out, note="（示例）")["kind"] == "fault")

    # ── ⑬ 状态词汇**只有一份**（N4：三态 ↔ 映射键、五态 ↔ STAGE_MARK 键，必须刚好对上）──
    # 🔴 2026-11-04 复审 N4：同一套状态原来在四处各写一遍字符串 —— `settle()` 收敛了**写法**，
    # **词汇**还靠人守同名：改一处状态名，其余三处**静默分叉**。
    # 现在词汇表在 `handoff/__init__`，这条守卫把"刚好覆盖"变成**会红**的规矩。
    print("\n⑬ 状态词汇只有一份：词汇表 ↔ 各处的键，刚好对上")
    from typing import get_args

    from proto.handoff import OutcomeStatus, TaskStage
    from proto.handoff.briefing import Task, _STATUS_TO_STAGE
    from proto.handoff.ledger import STAGE_MARK

    statuses, stages = set(get_args(OutcomeStatus)), set(get_args(TaskStage))
    check("三态 ↔ `_STATUS_TO_STAGE` 的键**刚好**相等",
          set(_STATUS_TO_STAGE) == statuses,
          f"映射键={sorted(_STATUS_TO_STAGE)}；三态={sorted(statuses)}")
    check("映射出来的值都是**合法的 TaskStage**",
          set(_STATUS_TO_STAGE.values()) <= stages,
          f"越界：{sorted(set(_STATUS_TO_STAGE.values()) - stages)}")
    check("五态 ↔ `STAGE_MARK` 的键**刚好**相等",
          set(STAGE_MARK) == stages, f"标记键={sorted(STAGE_MARK)}；五态={sorted(stages)}")
    # ⚠️ TypedDict 的 `__annotations__` 在 3.12 下是 `ForwardRef`（字符串的壳）——
    #    要 `get_type_hints` 解开再比，不然比的是壳。
    from typing import get_type_hints

    check("`Task.stage` 的标注就是 `TaskStage`（不是又一个 str）",
          get_type_hints(Task)["stage"] == TaskStage, repr(get_type_hints(Task)["stage"]))

    # ── ⑫ 交接包里**每个渲染器都调得通** ──
    # 🔴 这条是 2026-11-03 搬家时**真踩到**的坑：`_judge_material` 抽进 `handoff/judgement.py` 时
    # 漏带了它的模块级依赖 `AIMessage` —— 而"逐字比对"只验纯函数、**验不到模块级依赖**，
    # 单测也从来没调过它 → 一路绿到真跑才炸（`NameError`，整轮 0 秒中断）。
    # **搬完必须挨个调一遍**，这是最便宜的兜底。
    print("\n⑫ 交接包里的每个渲染器都调得通（搬家漏带依赖 → 这里红）")
    from langchain_core.messages import AIMessage as _AI

    from proto.handoff.ask import build_prompt as _ask
    from proto.handoff.judgement import _judge_material
    from proto.handoff.ledger import render_ledger as _ledger
    from proto.handoff.nudge import nudge_text as _nudge

    _fake_resp = {"messages": [_AI(content="执行器说的第一轮"), _AI(content="第二轮")]}
    material = _judge_material([], _fake_resp)
    check("⑫ `_judge_material`（无产物那条路）调得通", "第二轮" in material, material[:60])
    check("⑫ `_judge_material` 超长时**保头保尾 + 带标记**",
          "平台截断" in _judge_material([], {"messages": [_AI(content="头" * 3000 + "尾" * 3000)]}))
    check("⑫ `render_ledger` 调得通", "【平台提示】" in _ledger([short_task]))
    check("⑫ `nudge_text` 调得通", "任务还没完成" in _nudge(_Judgement(status="incomplete")))
    # 判据①：进度 / 缺失项是**判定说的**（第三方评审，它可能判错）—— 必须标出来，
    # 否则执行器会把"判定说你做完了"当成"事实是你做完了"。
    # ⚠️ 这条**在真 run 里可能压不到**（要有 incomplete 轮次才有 nudge）—— 所以在这里断。
    _nud = _nudge(_Judgement(status="incomplete", progress="做了一半", missing=["课时"]))
    check("⑫ `nudge_text` 标了**判定说**（判据①）",
          _nud.count("（判定说）") == 2, _nud)
    _led = _ledger([{**short_task, "stage": "waiting_for_input",
                     "progress": "做了一半", "missing": ["课时"]}])
    check("⑫ `render_ledger` 标了**判定说**（判据①）",
          "已做到（判定说）" in _led and "还缺（判定说）" in _led, _led)
    check("⑫ `ask.build_prompt` 调得通",
          "你的上下文" in _ask("quiz", "出题", ExecOutcome(status="waiting_for_input", report="r"),
                              [HumanMessage("x")]))

    # ── ⑬ 产物归属：**记账**，不扫文本（判据④结构化 / ⑤硬机制）──
    # 🔴 2026-11-03 第 5 步：原来是 `_recover_artifacts` **扫消息文本**猜"这次写了哪些产物"，
    # 再用 `preexisting` 差集补"读来的被算成写出来的" —— 既不是结构化、也不是硬机制，
    # 而且并发时差集还会互相覆盖。现在改成**每次 run 造一个自己的 write_artifact**（闭包）。
    print("\n⑬ 产物归属：记账，不扫文本")
    from proto import artifacts as _A
    from proto.artifacts import make_write_artifact, read_artifact
    from proto.artifacts import write_artifact as _main_writer

    _A.clear()
    mine: list[str] = []
    other: list[str] = []
    make_write_artifact(mine).func("甲", "s", "md", "正文" * 5)
    make_write_artifact(other).func("乙", "s", "md", "正文" * 5)
    check("⑬ 谁写的谁记账，且**按 run 分开**",
          len(mine) == 1 and len(other) == 1 and mine[0] != other[0], f"{mine} / {other}")
    n = len(mine)
    read_artifact.func(artifact_id=mine[0])
    check("⑬ **读**不记账 —— 「读来的算成写出来的」结构性不可能", len(mine) == n)
    _main_writer.func("丙", "s", "md", "正文" * 5)
    check("⑬ 主 agent 的实例不记账（它不需要按 run 归属）", len(mine) == n)
    # ⚠️ 查**代码**不是查注释 —— 注释里正解释着"原来是怎么扫的"，那两个词会出现
    _src = io.open("proto/executor.py", encoding="utf-8").read()
    check("⑬ `_recover_artifacts` / `preexisting` 已删（不再扫文本）",
          "def _recover_artifacts" not in _src and "preexisting = set(" not in _src)

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
