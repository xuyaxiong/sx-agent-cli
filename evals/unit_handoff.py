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

import ast
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

#: 切片形状：`[:X]`（切头）、`[-X:]`（切尾）—— **X 是任意表达式**。
#: **不**包含 `[N:M]` / `[N:]`（那是取段、不是截断）。
#:
#: 🔴 **2026-11-05 修（深度审计 §P2-2）**：原来只认**数字字面量**（`\[:\d+\]`）——
#: 于是 `flat[:limit]`、`text[-tail:]` 这种**按变量切**的写法**全部绕过**：
#: 守卫**根本没被触发**（不是"豁免了"）。等于一条从来不挡人的守卫，
#: 而它守的正是用户点过名的那条病（交接内容被悄悄切一刀）。
#: 放宽之后代价是**豁免要写全** —— 每一条都得写明"为什么这个切法不算悄悄切"。
SLICE = re.compile(r'\[\s*:\s*[^\[\]:]+\]|\[\s*-[^\[\]:]*:\s*\]')

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
    'proto/handoff/briefing.py': [
        (r'flat\[:limit\]',
         '`_one_line` 的**索引行**：它是「清单 + 按需」那一层（不是"悄悄切一刀"）—— '
         '切了带 `…`，块尾写明用 `read_user_turn(轮次)` 取全文'),
        (r'done_turns\[:\s*len\(t_turns\)\]',
         '`moot_older_waiting` 的**前缀比较**（"用户又说过话"的判据）—— 不产生任何新文本，'
         '比的是两份原话快照的前几轮是否逐字相同'),
    ],
    'proto/artifacts.py': [
        (r'\)\[:n\]', '**落盘文件名**的 slug 长度（不是交接内容；文件名截断不影响可读性）'),
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
        "report": LONG,
        "qa": [{"by": "用户", "q": LONG, "a": LONG}],
        "user_turns": [{"n": 1, "text": TURN, "after": LONG}],
    }
    brief = render_briefing(task)
    check("④ 交接书 · 任务全文在", task["task"] in brief)
    check("④ 交接书 · 执行器上次说的全文在", LONG in brief)
    # ⚠️ 原来这里还有一条「缺什么全文在」（`task["missing"][0] in brief`）——
    #    2026-11-05 判定退休，`missing` 没有来源了，这条**是在断言一个不存在的字段**：
    #    `task` 是上面那个 fixture 字典，`task["missing"][0]` 就是 `LONG`，
    #    而上一行刚断过 `LONG in brief`（由 `report` 满足）—— 它一直靠 report 假绿。
    #    删掉，不留"看着还有一条"的错觉。
    # 🔴 **用户原话走的是「清单 + 按需」**（不是"不截"）：索引行在、正文按需 ——
    # 见 `briefing.turn_lines` 的分层。超长输入下**常驻层不许跟着涨**。
    check("④ 交接书 · 用户原话**索引行**在（序号 + 字数 + 开头）",
          f"第 1 轮（{len(TURN)} 字）" in brief and " ".join(TURN.split())[:40] in brief)
    check("④ 交接书 · 超一行的原话**正文没进上下文**（按需 `read_user_turn`）",
          TURN not in brief and "read_user_turn" in brief)
    check("④ 交接书 · 「它在回答的上一句」索引行在", "它在回答的上一句" in brief)
    check("④ 交接书 · 问答对全文在", task["qa"][0]["a"] in brief)

    # ── ⑧ 推回去（平台 → 执行器）──
    nudge = _nudge_text()
    check("⑧ 推回去 · **两条出路都点明了**（交付 / ask_user）",
          "write_artifact" in nudge and "ask_user" in nudge)

    # ── ⑨ 试答（平台 → 主 agent）──
    # ⚠️ `asked` 与 `report` **故意不同**：试答提示词里那两行取自**两个字段**
    #    （"它要的信息" ＝ `ask_user` 的参数原话；"它最后说的那段" ＝ `report`）。
    #    两处共用一个值的话，"两行其实同源"这件事在断言上**看不出来** ——
    #    而原来那条 `LONG in ask` 正是这么假绿的（名字叫"执行器问的"，满足它的是 `report`）。
    ASKED_LONG = "这一条是它申报要的信息：" + "要素。" * 100
    outcome = ExecOutcome(status="waiting_for_input", artifacts=[], report=LONG, rounds=1,
                          asked=ASKED_LONG)
    ask = _ask_prompt("s", "超长任务：" + "细节。" * 200, outcome,
                      [HumanMessage(LONG), AIMessage(LONG)])
    check("⑨ 试答 · 执行器**申报要的那句原话**全文在", ASKED_LONG in ask)
    check("⑨ 试答 · 它最后说的那段（`report`）全文在", LONG in ask)
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
                   # ⚠️ **长的那一份放在 `asked` 上**（2026-11-04）：台账 waiting 那档
                   #    现在打的是 `asked`（"它要的信息"），不是 `report`（那段过场）——
                   #    见 `ledger.render_ledger`。截断这一列要测的是**它实际打的那个字段**。
                   "asked": LONG,
                   "qa": [], "user_turns": []}],
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
    # ⚠️ **两个字段都要缩短**：台账 waiting 那档打的是 `asked`（不是 `report`），
    #    只缩 `report` 的话 `asked` 还是长的 → 照样被截 → 这条会假红（2026-11-04 踩过）。
    short_task = {**st["tasks"][0], "report": "很短", "asked": "用几课时？"}
    check("② 台账 · 短内容**不该**被标截断",
          "【平台截断" not in node_prepare({**st, "tasks": [short_task]})["messages"][0].content)

    # ── ⑪ 透传（平台 → 用户）：**不再注入消息** —— 问题只住在通道里 ──
    # 🔴 **2026-11-04（F18）**：原来这里注入一条 `AIMessage`（内容＝执行器的原话），
    #    靠 `additional_kwargs["injected_by"]` 让**记录层**知道"不是主 agent 说的"。
    #    ⚠️ 那个标记**不漏给模型** —— 于是模型在自己的上下文里看到
    #    `AIMessage: 【lesson-prep】问：…`，**以为是自己说过的话**。
    #
    #    🔴 **2026-11-04 收口**：试过撤掉这条注入 —— **实测无效**（撤了主 agent 照样
    #    复述 4/4，因为问题还从台账和回执进来）。真正治它的是 `agent._StopWhenPending`
    #    （执行器一申报待答，主 agent 那一拍**根本不生成**）。
    #    ⇒ 注入**留着**：它是转写里唯一那条问题，也是 `_user_turns` 的
    #    「它在回答的上一句」的来源。**问题出在"它有机会说"，不在"它看得见"。**
    from proto.graph import node_finalize, render_relay

    fin = node_finalize({**st, "tasks": [short_task]})["messages"][0]
    tag = (fin.additional_kwargs or {}).get("injected_by")
    check("⑪ 透传 · 注入了 AIMessage（转写里那条问题）", type(fin).__name__ == "AIMessage")
    check("⑪ 透传 · 打了 `injected_by` 标记", tag == "platform.finalize", f"tag={tag!r}")
    from proto.handoff import INJECTED
    from evals.harness import _injected_label

    check("⑪ 透传 · 标记在词汇表里声明过", tag in INJECTED, f"未声明：{tag!r}")
    check("⑪ 透传 · 记录层标的是**平台透传**（不是主 agent 输出）",
          "透传" in _injected_label(tag) and "不是主 agent" in _injected_label(tag),
          _injected_label(tag))
    check("⑪ 透传 · 没标记的消息**不**被误标", _injected_label(None) == "")

    # 🔴 **贴的是"它申报的那句原话"，不是执行器那段独白**（2026-11-05，`solution.md` §0.65
    #    Y1 第一步）：`ask_user` 的参数原来**只活在当轮**，一进"待答"就丢了 ✗ ——
    #    跨轮只剩 `report`（混着"好的，我先来进行第一步的分析…"之类过场）。
    #    用户要答的是**问题**，不是那段独白。
    # ⚠️ 断言断的是**纯函数** `render_relay`（注入那条消息就是它的产物）——
    #    抽出来是为了可单测，同 `ask.build_prompt` / `nudge.nudge_text` 的形状。
    _w = {**short_task, "skill": "lesson-prep", "asked": "这节课希望侧重哪种上法？",
          "report": "好的，我先来进行第一步的分析，然后进入第二步——生成前确认。\n\n"
                    "## 第一个问题\n这节课希望侧重哪种上法？"}
    _relayed = render_relay([_w])
    check("⑪ 透传 · 贴的是**申报的原话**（`Task.asked`）",
          "「lesson-prep」问：这节课希望侧重哪种上法？" in _relayed, _relayed)
    check("⑪ 透传 · 执行器那段独白**不进**透传",
          "我先来进行第一步的分析" not in _relayed, _relayed)
    _relayed2 = render_relay([{**_w, "asked": ""}])
    check("⑪ 透传 · `asked` 缺失时**显式说清**（不静默降级、也不假装有）",
          "没留下问题原话" in _relayed2 and "我先来进行第一步的分析" in _relayed2, _relayed2)
    # 注入的那条 == 纯函数渲染出来的（**一处定义**，别让两条路漂开）
    check("⑪ 透传 · 注入的消息内容 == `render_relay` 的输出",
          fin.content == render_relay([short_task]), f"注入：{fin.content[:60]!r}")

    # 🔴 同一刻还要写**结构化动作**（2026-11-04）：断言读它，不读 `log` 那句人话 ——
    #    见 `State.actions` 的注释。拿掉这一条 → `unit_feed_waiting` ⑨ 的守卫会红。
    fin_out = node_finalize({**st, "tasks": [short_task]})
    acts = fin_out.get("actions") or []
    check("⑪ 透传 · 写了结构化动作（`did=relay` + 涉及哪些 Task + 多少字）",
          [a.get("did") for a in acts] == ["relay"]
          and acts[0].get("tasks") == [short_task["id"]]
          # ⚠️ 比的是**渲染出来的那段文本**的长度 —— 它现在不进 messages，
          #    所以拿 `render_relay` 现算（原来读 `fin_out["messages"][0].content`）。
          and acts[0].get("chars") == len(render_relay([short_task])),
          repr(acts))
    check("⑪ 透传 · 同一刻那句**人话仍在** `log` 里（给人看的，没被删）",
          any("透传" in ln for ln in (fin_out.get("log") or [])), repr(fin_out.get("log")))

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

    # ── ⑮ **三态由结构定**（判定退休，2026-11-05）—— 全系统唯一决定"任务往哪走"的地方 ──
    # 🔴 判定那次 LLM 调用退休后，"完成没"不再是**判出来的**，而是**平台看得见的三件事**：
    #    申报要信息 / 交了产物 / 都没有。这条把它钉住（原来内联在 `run_task` 里，测不到）。
    print("\n⑮ 三态由结构定：申报要信息 / 交了产物 / 都没有")
    from proto.executor import ExecOutcome as _EO, _structural_status as _st

    check("申报了要信息 → `waiting_for_input`（哪怕也交了产物 —— **申报优先**）",
          _st(_EO(asked="用几课时？", artifacts=["art_001"])) == "waiting_for_input")
    check("交了产物、没申报 → `completed`",
          _st(_EO(artifacts=["art_001"])) == "completed")
    check("都没交也没申报 → `incomplete`（推回去）",
          _st(_EO(report="我说了一堆话")) == "incomplete")
    check("**只说了话不算交付**（正文交付是反模式）—— 仍是 `incomplete`",
          _st(_EO(report="成果都在我这段话里了" * 20)) == "incomplete")

    # ── ⑮b **判定记录**：给它的上下文 / 结果 / 依据 —— 三样都在**一条**记录里 ──
    # 🔴 用户 2026-11-05：*"对于完成没，**给它的上下文是啥、结果又是啥、产生结果依据又是啥**
    #    —— 这些都要能看到才能审计，不然我咋知道怎么调优。"*
    #    判定那次 **LLM 调用**退休了，但**这个判断还在** —— 记录不能跟着退休
    #    （原来只剩"交付 N 个产物 → 收工"＋"status=…"两行 note：**"没申报"是隐式的**，
    #      "依据"要读的人自己从箭头里猜）。
    print("\n⑮b 「任务完成没」的判定记录：上下文 / 结果 / 依据，三样齐全")
    from proto.executor import _verdict as _vd
    from proto.handoff.decision import render_decision as _rd

    _verdict_evs: list[dict] = []
    trace.subscribe(_verdict_evs.append)

    def _rec(o: object) -> str:  # noqa: ANN202
        """走**真路**：`_verdict` → `trace.decision` → 事件 → `render_decision`（两层共用那一处）。"""
        trace.decision(**_vd(o, "R01"))  # type: ignore[arg-type]
        return _rd(_verdict_evs[-1])

    _done = _rec(_EO(status="completed", artifacts=["art_001"]))
    check("**上下文** = 三件结构事实，逐条在（申报 / 产物账 / 轮次）",
          "申报要信息" in _done and "交付产物" in _done and "art_001" in _done
          and "轮次" in _done, _done)
    check("**没申报**这件事也看得见（隐式的事实审计不了）",
          "**没有**（本轮没调它）" in _done, _done)
    check("**结果** = 三态 ＋ **依据**（命中的是哪一条）",
          "completed" in _done and "依据：它**交了产物**" in _done, _done)
    check("**下一步**说清谁来接（回执给主 agent；平台不判质量）",
          "回执给主 agent" in _done and "平台不判质量" in _done, _done)
    # 🔴 **来源标注不许串**（这一格是**平台自己的结论**，不是 LLM 的话）——
    #    借【结果】那个段名就会标成"**它说的** —— LLM 的输出"，那是**假话**。
    check("第三个格子是【平台判定】（平台的话**不标**来源）",
          "【平台判定】" in _done and "【结果】" not in _done, _done)
    check("**没有**「它说的 / LLM 的输出」这种标注（平台的话自证没意义）",
          "它说的" not in _done and "LLM 的输出" not in _done, _done)
    _ask = _rec(_EO(status="waiting_for_input", asked="用几课时？", artifacts=["art_001"]))
    check("**申报优先**：交了产物也报 `waiting_for_input`，依据说的是**申报**",
          "waiting_for_input" in _ask and "依据：它**申报了要信息**" in _ask, _ask)
    _none = _rec(_EO(status="incomplete", report="成果都在我这段话里了"))
    check("都没有 → `incomplete`，依据说的是**没命中任何一条**",
          "incomplete" in _none and "既没申报、也没交产物" in _none, _none)
    _fault = _rec(_EO(status="incomplete", fault="TimeoutError: （模拟）"))
    check("平台故障：依据和下一步都换成故障那条（不说「执行器干不了」）",
          "平台故障" in _fault and "原样再 `delegate`" in _fault, _fault)
    # ⚠️ **跑一遍真 `run_task`**（假模型 ＋ 假 agent，**不调 LLM、秒级**）：源码级断言只能证明
    #    "那一行写着"，证明不了"**跑得到**"（名字拼错 / 顺序不对 —— 真跑才炸）。
    #    假 agent **只做一件事**：调真的 `write_artifact` —— `created` 是 `run_task` 的闭包，
    #    只有真工具能记账（假的 state 里塞不出产物来）。
    from langchain_core.messages import AIMessage as _AIM

    class _FakeAgent:
        def __init__(self, tools: list) -> None:
            self._tools = {t.name: t for t in tools}

        def stream(self, state, config, stream_mode):  # noqa: ANN001, ANN201
            self._tools["write_artifact"].invoke({
                "title": "《荷花》教案", "summary": "三年级下册完整教案",
                "file_type": "md", "content": "正文" * 40})
            yield ("values", {"messages": [*state["messages"], _AIM("教案已交付")]})

    _create, _model = _ex.create_agent, _ex.get_model
    _ex.get_model = lambda *a, **k: object()  # type: ignore[assignment]
    _ex.create_agent = lambda model, tools=None, **k: _FakeAgent(tools or [])  # type: ignore[assignment]
    _n_before = len(_verdict_evs)
    try:
        _out2 = _ex.run_task(_Skill(name="lesson-prep", description="备课", instructions="写教案"),
                             "生成《荷花》教案")
    finally:
        _ex.create_agent, _ex.get_model = _create, _model  # type: ignore[assignment]
    _run_rec = [e for e in _verdict_evs[_n_before:] if e["type"] == "decision"]
    check("`run_task` **真跑一遍**（completed 那一档）→ 判定记录真的发出来了",
          len(_run_rec) == 1 and _run_rec[0].get("result_by") == "platform",
          f"发了 {len(_run_rec)} 条")
    # ⚠️ **不许写死 `art_001`**：同一个进程里前面已经写过产物，这一趟的号是递增的
    #    （实测踩到：断 `art_001` 当场红 —— 那正是回执那边"提示写死 art_001"的同族毛病）。
    check("走完这一趟，记录里是**这一趟真写进去的那个产物 ID**",
          _out2.status == "completed" and bool(_out2.artifacts)
          and _out2.artifacts[0] in _rd(_run_rec[0]),
          f"产物={_out2.artifacts}；记录={_rd(_run_rec[0]) if _run_rec else '（没发）'}")
    # ⚠️ **试答那条（LLM 的）照旧**：同一套记录形状，只有第三个格子换了段名和来源标注。
    _llm = _rd({"who": "试答（LLM）· 平台强制的一步", "given": "g", "q": "q",
                "result": "can_answer = **False**", "next": "n"})
    check("**试答**那条不受影响：第三个格子仍是【结果】＋「**它说的**」",
          "【结果】" in _llm and "**它说的**" in _llm, _llm)
    try:
        trace.decision("x", q="q", result_by="platfrom")   # 故意的错拼
        _blew = False
    except ValueError:
        _blew = True
    check("`result_by` 打错就**当场炸**（不静默退回 `llm` —— 那会把平台的结论标成它说的）",
          _blew)

    # ── ⑬ 状态词汇**只有一份**（N4：三态 ↔ 映射键、五态 ↔ STAGE_MARK 键，必须刚好对上）──
    # 🔴 2026-11-04 复审 N4：同一套状态原来在四处各写一遍字符串 —— `settle()` 收敛了**写法**，
    # **词汇**还靠人守同名：改一处状态名，其余三处**静默分叉**。
    # 现在词汇表在 `handoff/__init__`，这条守卫把"刚好覆盖"变成**会红**的规矩。
    print("\n⑬ 状态词汇只有一份：词汇表 ↔ 各处的键，刚好对上")
    from typing import get_args

    from proto.handoff import OutcomeStatus, TaskStage
    from proto.handoff.briefing import Task, _STATUS_TO_STAGE, settle
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

    # ── ⑬b **落账记一行**：`Task.stage` 在 trace 里看得见 ──
    # 🔴 2026-11-05：三态是"**这一趟**跑成什么样"，`Task.stage` 才是"**这个任务**现在算什么" ——
    #    后者原来只活在 state 里（终端那行 `📋 TASKS` 是拿快照现渲染的），
    #    trace 里**没有**"tac2700f3 → completed"这一行，复盘时接不上
    #    （用户要审计的"完成没"里，这就是**任务那一侧的结果**）。
    print("\n⑬b 落账记一行：`Task.stage` 在 trace 里看得见（原来只活在 state 里）")
    _evs: list[dict] = []
    trace.subscribe(_evs.append)
    _task_rec: dict = {"id": "tac2700f3", "skill": "quiz"}
    _stage = settle(_task_rec, _EO(status="completed", artifacts=["art_001"]))
    _line = next((str(e.get("text")) for e in _evs if "Task 落账" in str(e.get("text"))), "")
    check("落账行里有 **Task id**（trace 里能把任务接上）", "tac2700f3" in _line, _line)
    check("**三态 → stage** 的映射结果写出来了（唯一映射的落点）",
          _stage == "completed" and "stage=completed" in _line, _line)
    check("**无 kind** = 纯记录、不上屏（终端那行快照已经在说同一件事）",
          next((e.get("kind") for e in _evs if "Task 落账" in str(e.get("text"))), "?") == "",
          repr(next((e for e in _evs if "Task 落账" in str(e.get("text"))), None)))

    # ── ⑬+ 替身只许用**真类型里有的字段** ──
    # 🔴 2026-11-05：判定退休把 `progress` / `missing` 从 `Task` / `ExecOutcome` 上摘了，
    #    而**替身**（单测 fixture 字典 / case 注入的 `initial_tasks` / 手捏的假 outcome 类）
    #    没跟着走 —— 实测**替身烂了 11 处**（另有两处同族：一行死代码、一条空转断言），
    #    **全是绿的**：多一个键没人读、少一个键没人问，正是"看着还有一条"的错觉
    #    （也违反"一套，别分叉"）。规矩只靠记性必然烂 → 进守卫。
    #    ⚠️ **射程**（别当成万能）：整份 Task 字面量 / 展开式（`{**task, "x": …}`）/
    #    case 注入 / 假 outcome 类。**不带 `id`/`stage` 的局部 delta**
    #    （`task.update({"progress": …})`）射不到 —— 那种只能靠"像不像 Task"猜，
    #    猜宽了会误伤别的字典（`id` / `task` / `report` 都是很普通的键名）。
    print("\n⑬+ 替身只许用真类型里有的字段")
    _tf = set(get_type_hints(Task))
    _stray: list[str] = []
    _fakes: list[str] = []
    _n_dict = _n_case = 0
    for _p in sorted(glob("evals/unit_*.py")):
        for _n in ast.walk(ast.parse(io.open(_p, encoding="utf-8").read())):
            # ① 长得像 Task 的字典字面量（同时有 `id` 和 `stage` 两个键）
            if isinstance(_n, ast.Dict):
                _k = {x.value for x in _n.keys
                      if isinstance(x, ast.Constant) and isinstance(x.value, str)}
                if {"id", "stage"} <= _k:
                    _n_dict += 1
                    if _k - _tf:
                        _stray.append(f"{_p}:{_n.lineno} → {sorted(_k - _tf)}")
                # ①' **展开式**（`{**task, "report": …}`）—— 显式键里已经有一个 Task 字段，
                #     其余显式键就必须也是（`{**st, "tasks": [...]}` 那种不算：
                #     `tasks` 不是 Task 字段，`_k & _tf` 是空）
                elif any(x is None for x in _n.keys) and (_k & _tf) and (_k - _tf):
                    _stray.append(f"{_p}:{_n.lineno} → {sorted(_k - _tf)}（展开式）")
            # ② 手捏的 outcome 替身：类属性里同时有 `status` / `report` 的就是
            if isinstance(_n, ast.ClassDef):
                _a = {t.id for _s in _n.body if isinstance(_s, ast.Assign)
                      for t in _s.targets if isinstance(t, ast.Name)}
                if {"status", "report"} <= _a:
                    _fakes.append(f"{_p}:{_n.lineno} → {_n.name}")
    # ③ case 注入的 `initial_tasks`（`evals/_child.py` 原样塞进 state）—— 一样会被静默忽略
    import yaml as _yaml

    for _p in sorted(glob("evals/cases/*.yaml")):
        _case = _yaml.safe_load(io.open(_p, encoding="utf-8").read()) or {}
        for _t in _case.get("initial_tasks") or []:
            _n_case += 1
            if set(_t) - _tf:
                _stray.append(f"{_p} · initial_tasks → {sorted(set(_t) - _tf)}")
    # 🔴 **有效性自检**（同 `report.VALIDITY_CHECKS` 的道理）：扫不到东西的话，
    #    上面两条会**永远绿着**（路径写错、glob 空、fixture 换了形状 —— 都不会报错）。
    check(f"确实扫到了替身（不是空转）：fixture 字典 {_n_dict} 个 / case 注入 {_n_case} 条",
          _n_dict > 0 and _n_case > 0)
    check("fixture / case 里没有 `Task` 之外的键", not _stray, "\n".join(_stray))
    check("没有手捏的 outcome 替身（用真 `ExecOutcome`）", not _fakes, "\n".join(_fakes))

    # ── ⑫ 交接包里**每个渲染器都调得通** ──
    # 🔴 这条是 2026-11-03 搬家时**真踩到**的坑：`_judge_material` 抽进 `handoff/judgement.py` 时
    # 漏带了它的模块级依赖 `AIMessage` —— 而"逐字比对"只验纯函数、**验不到模块级依赖**，
    # 单测也从来没调过它 → 一路绿到真跑才炸（`NameError`，整轮 0 秒中断）。
    # **搬完必须挨个调一遍**，这是最便宜的兜底。
    print("\n⑫ 交接包里的每个渲染器都调得通（搬家漏带依赖 → 这里红）")
    from langchain_core.messages import AIMessage as _AI

    from proto.handoff.ask import build_prompt as _ask
    from proto.handoff.ledger import render_ledger as _ledger
    from proto.handoff.nudge import nudge_text as _nudge

    _fake_resp = {"messages": [_AI(content="执行器说的第一轮"), _AI(content="第二轮")]}
    check("⑫ `render_ledger` 调得通", "【平台提示】" in _ledger([short_task]))
    check("⑫ `nudge_text` 调得通", "两条路" in _nudge())
    # 判据①：进度 / 缺失项是**判定说的**（第三方评审，它可能判错）—— 必须标出来，
    # 否则执行器会把"判定说你做完了"当成"事实是你做完了"。
    # ⚠️ 这条**在真 run 里可能压不到**（要有"没交也没问"的轮次才有 nudge）—— 所以在这里断。
    # 🔴 2026-11-05 判定退休：nudge **不再带判定的 progress/missing**（没有来源了），
    #    改成点明**两条出路**（交付 / `ask_user`）—— 这是"平台造路"的正面形式。
    _nud = _nudge()
    check("⑫ `nudge_text` 点明了**两条出路**（`write_artifact` / `ask_user`）",
          "write_artifact" in _nud and "ask_user" in _nud, _nud)
    # ⚠️ 待答那档的标签是「它要的信息」（打 `asked`），**不是**「执行器说」（打 `report`）——
    #    2026-11-04 改的（F18 同批）：主 agent 需要知道的是"**它在等什么**"，
    #    而这段文本还是 `finalize` 不再注入之后**它唯一能看到问题内容的地方**。
    #    这一列断的是"台账里**只有它自己的话**，没有第三方（判定）的话"—— 换标签不影响这条。
    _led = _ledger([{**short_task, "stage": "waiting_for_input",
                     "asked": "它在问课时", "report": "好的，第一个问题已发出"}])
    check("⑫ `render_ledger` 里**只有它自己的话**（判定的两行随判定退休删了）",
          "它要的信息" in _led and "判定说" not in _led, _led)

    # 🔴 **平台故障要压过 `stage` 标记**（2026-11-05 修 A3）：故障的 Task `stage` 也是
    #    `failed`，照 stage 说话就是假话（"❌ 它没交付、也没说要什么" ＝ 执行器没干成），
    #    而主 agent 做**下一轮决定**时看的正是台账 —— 同一件事两副面孔。
    _l1 = _ledger([{**short_task, "stage": "failed", "report": "",
                    "fault": "TimeoutError: （模拟）抖动"}])
    check("⑫ 故障的 Task 说「平台故障」，不说「没交付」",
          "平台故障" in _l1 and "没交付" not in _l1, _l1)
    check("⑫ 故障时**不打**「执行器说」（它一个字都没说，照打就是冒号后一行空白）",
          "执行器说" not in _l1, _l1)
    check("⑫ 故障原因原样带出来（复盘要看）", "TimeoutError" in _l1, _l1)
    # 反向：**没有故障的 failed 别跟着改口**（不然就把"执行器干不成"也盖掉了）
    _l2 = _ledger([{**short_task, "stage": "failed", "fault": "",
                    "report": "我尽力了但没做成"}])
    check("⑫ 不是故障的 failed 照旧说「执行器说」",
          "执行器说" in _l2 and "平台故障" not in _l2, _l2)
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
