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
        (r'based_on_turns=\[1, 3\]', '文档字符串里的示例'),
    ],
    # ⚠️ **`proto/handoff/` 是包，`glob('proto/*.py')` 扫不到它** ——
    # 2026-11-03 把交接的 schema 与模板搬进包里时差点让它掉出守卫的覆盖面。
    'proto/handoff/briefing.py': [
        (r'q_lines\[0\]|q_lines\[1:\]', '按行拆分渲染，不丢字'),
    ],
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
    from proto.artifacts import ARTIFACTS, store_artifact
    from proto.executor import ExecOutcome
    from proto.handoff.judgement import _Judgement
    from proto.handoff.nudge import nudge_text as _nudge_text
    from proto.graph import node_prepare

    print("\n② 逐个交接点：喂超长输入，断**一个字都没丢**")

    # ── ④ 交接书（平台 → 执行器）──
    ARTIFACTS.clear()
    aid = store_artifact("超长产物", "梗概" * 40, "md", "正文" * 900)
    task = {
        "id": "t1", "skill": "s", "task": "超长任务描述：" + "细节。" * 200,
        "stage": "waiting_for_input", "artifacts": [aid], "refs": [aid],
        "report": LONG, "progress": LONG, "missing": [LONG],
        "qa": [{"by": "用户", "q": LONG, "a": LONG}],
        "user_turns": [{"n": 1, "text": LONG, "after": LONG}],
        "based_on": [1],
    }
    brief = render_briefing(task)
    check("④ 交接书 · 任务全文在", task["task"] in brief)
    check("④ 交接书 · 执行器上次说的全文在", LONG in brief)
    check("④ 交接书 · 判定给的进度全文在", task["progress"] in brief)
    check("④ 交接书 · 缺什么全文在", task["missing"][0] in brief)
    check("④ 交接书 · 用户原话全文在（点名的那轮）", task["user_turns"][0]["text"] in brief)
    check("④ 交接书 · 它在回答的上一句全文在", task["user_turns"][0]["after"] in brief)
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
    ARTIFACTS.clear()
    st = {
        "messages": [AIMessage("（没有 HumanMessage，免得触发投递）")],
        "tasks": [{"id": "t1", "skill": "s", "stage": "waiting_for_input",
                   "artifacts": [], "refs": [], "report": LONG,
                   "progress": LONG, "missing": [LONG], "qa": [], "user_turns": []}],
    }
    injected = node_prepare(st)["messages"][0].content
    check("② 台账 · 长内容被截了（它是每轮重复注入的索引，允许短）", LONG not in injected)
    check("② 台账 · **但截了必须说**：带了平台截断标记", "【平台截断" in injected)
    check("② 台账 · 说明了还差多少字 / 去哪看全文",
          "全文见那次的回执" in injected)
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
    from proto.artifacts import ARTIFACTS as _A
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
