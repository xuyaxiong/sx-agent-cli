"""单元级：待答投递必须**累积历次答复**（不调 LLM）。

    uv run python -m evals.unit_feed_waiting

**为什么要有它**（2026-10-29 人在环实测暴露）：

执行器是隔离上下文、**用完即弃** —— 每次投递都是一次全新的 `run_task`。
`_feed_waiting` 原来只把**最新一条**用户消息拼进提示词，于是用户"一次答一个"时：

    问「用途」→ 答「课前预习」→ 问「填写形式」→ 答「A」→ **又问一遍「用途」**

答第二个就把第一个冲掉了，用户被同一个问题问两遍。**脚本化跑批测不出来**：
预置答复是一句话把两问都答了（"…用在课前预习，填空形式…"），恰好绕开了它。

这里把 `run_task` 换成记录器，直接看**待投递的提示词里有什么** —— 确定性、毫秒级、
不依赖模型是否"一次只问一个"。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import ast
import asyncio
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
os.environ["PROTO_LOG_TAG"] = "unit-feed-waiting"
os.environ["PROTO_LOG_DIR"] = "tmp/logs"   # 🔴 测试日志是**噪音** —— 别混进给人看的 `logs/`

from langchain_core.messages import HumanMessage

import proto.agent
import proto.executor
from proto import graph
from proto.executor import ExecOutcome

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail else ""))


#: 待答时执行器"最后说的那句" —— 断言要拿它当期望值（见 ⑧）
WAITING_REPORT = "（还需要信息）"

#: 它**申报**要的那句原话（`ask_user` 的参数）—— 🔴 与 `WAITING_REPORT` **故意不同**：
#: 【问答经过】的「问」必须取**这一句**，不是那段过场。两处用同一个值的话，
#: "取错了源"这件事在这条断言上**看不出来**（2026-11-04 日志定位的正是这个错）。
WAITING_ASKED = "这份导学单用在哪？（课前预习 / 课中探究 / 课后巩固）"


def _waiting(report: str = WAITING_REPORT, asked: str = WAITING_ASKED) -> ExecOutcome:
    """投递后仍待答（不关心内容，只看提示词）。

    🔴 **用真的 `ExecOutcome`，不自己捏一个假类**（2026-11-05）：假类是**同一语义的第二份定义**
    （"一套，别分叉"），实测已经漂了 —— 它留着判定时代的 `progress` / `missing`（真类早删了），
    又缺 `asked` / `fault`（真类新加的）。多一份定义 = 多一次静默分叉的机会。
    """
    return ExecOutcome(status="waiting_for_input", artifacts=[], report=report, rounds=1,
                       asked=asked)


def _no_delivery() -> ExecOutcome:
    """推满轮次、这一轮什么都没交出来 —— 见 ⑥。

    ⚠️ 原来这个假类叫 `_CompletedNoArtifact`（`completed` + 空产物），**那个状态现在不可达**：
    三态由**结构**定，`artifacts` 空就不可能是 `completed`（`executor._structural_status`）。
    真正可达的同形状是 `incomplete`（推满轮次）—— 上一轮的产物**同样**不能被这一轮抹掉，
    ⑥ 要守的那条不变量一个字没变，只是换到了**走得通**的那条路上。
    """
    return ExecOutcome(status="incomplete", artifacts=[], report="（这一轮没交出东西）", rounds=3)


def _run_feed(state):
    """`_feed_waiting` 现在是**协程** —— 测试里包一层同步收口（asyncio 迁移第 2 步）。"""
    return asyncio.run(graph._feed_waiting(state))


def _run_node_feed(state):
    """同上：`node_feed` 现在是协程。"""
    return asyncio.run(graph.node_feed(state))


def _run_node_prepare(state):
    """⚠️ `node_prepare` **还是同步的**（它没有 I/O，纯拼串）—— 原样调就行。

    迁移到纯 asyncio 时它也会变成 `async def`（图里不该有同步节点），但那一步
    **不影响行为**，所以留到最后。
    """
    return graph.node_prepare(state)


def _stub_returns(value):
    """`run_task` 的桩：不管参数是什么都返回 `value`。

    ⚠️ 得是 **async** —— `run_task` 现在是协程（asyncio 迁移第 2 步）。
    """
    async def _f(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return value
    return _f


def _capture() -> list[tuple[str, list | None]]:
    """把 `run_task` 换成记录器，返回它每次收到的 **（提示词, 用户原话）**。

    ⚠️ `run_task` 是**模块属性**调用（`executor.run_task(...)`）—— 两条路都是，
    所以改这一处就够（原来是值导入，得两处各改一次）。
    """
    seen: list[tuple[str, list | None]] = []

    async def fake(skill, briefing: str = "", user_turns=None):  # noqa: ANN001, ANN202
        seen.append((briefing, user_turns))
        return _waiting()

    proto.executor.run_task = fake  # type: ignore[assignment]
    # 🔴 **试答那一步也要打桩**（2026-11-04 E1 之后，投递路径也会走它）：
    #    不打桩的话，每个"仍待答"的 section 都会**真调一次模型** ——
    #    **实测踩过**：忘了打桩 → 一个 section 多等 4.4 秒、还真花 token（单测必须不调 LLM）。
    #    要测"试答答得上"那条，自己在那一节里改成返回一句答案（见 ⑧）。
    proto.agent._ask_main = _stub_returns("")  # type: ignore[assignment]
    return seen


def main() -> None:
    print("① 用户第二次答复时，第一次的答复必须还在提示词里")
    seen = _capture()
    state = {
        "messages": [HumanMessage("篇幅一页")],
        # 🔴 **标识**（`solution.md` §0.65 Y1）：这句在回答哪个待答任务 ——
        #    没有它**一条都不投**（那是主 agent 的输入）。值 = 下面那个待答 Task 的 id。
        "answer_to": "t1",
        "tasks": [
            {
                "id": "t1",
                "skill": "guided-learning",
                "task": "生成一份导学单",
                "stage": "waiting_for_input",
                "artifacts": [],
                "refs": [],
                # 🔴 问答**成对**存（用户 2026-11-02）：`report` 是覆盖写的，
                # 只存答案的话第二次投递后旧答案就没有指涉了 —— 见 `Task.qa`
                "qa": [{"by": "用户", "q": "这份导学单用在哪？", "a": "课前预习"}],
            }
        ],
    }
    _run_feed(state)
    prompt = seen[0][0] if seen else ""
    check("投递发生了", bool(seen))
    check("带上本次答复", "篇幅一页" in prompt, prompt[-200:] if prompt else "（没投递）")
    check("**也带上历次答复**（这条是回归点）", "课前预习" in prompt, prompt[-200:] if prompt else "")

    print("\n② 主 agent 之前补的信息也要带上（同类问题：用完即弃会被冲掉）")
    seen = _capture()
    state["tasks"][0]["qa"] = [
        *state["tasks"][0]["qa"],
        {"by": "主 agent", "q": "这份导学单用在哪？", "a": "用户之前说过是三年级"},
    ]
    _run_feed(state)
    prompt = seen[0][0] if seen else ""
    check("带上主 agent 补的那条", "用户之前说过是三年级" in prompt, prompt[-240:] if prompt else "")
    check("**两条都在**（历次累积，不覆盖）", "课前预习" in prompt, prompt[-240:] if prompt else "")

    print("\n③ 可读产物清单照旧要带上（否则续跑时执行器看不见前序产物）")
    from proto import artifacts
    from proto.artifacts import store_artifact

    artifacts.clear()
    readable = store_artifact("《白桦》教案", "四年级诗歌教学", "md", "教案正文" * 20)
    seen = _capture()
    state["tasks"][0]["refs"] = [readable]
    _run_feed(state)
    prompt = seen[0][0] if seen else ""
    check("可读产物进了交接书", readable in prompt and "《白桦》教案" in prompt, prompt[-260:])

    print("\n④ 投递的提示词**就是** `render_briefing(rec)`（三条路径共用同一份交接书）")
    from proto.handoff.briefing import render_briefing

    seen = _capture()
    state["tasks"][0].update(
        {
            "report": "我先设计了目标……请问用在哪？",
            # 🔴 **`asked` 与 `report` 故意不同**：下面那条断言要能分辨"取错了源"
            #    （2026-11-04 修的就是这个 —— 原来取的是 `report`）。
            "asked": "这份导学单用在哪？（课前预习 / 课中探究 / 课后巩固）",
        }
    )
    _run_feed(state)
    prompt = seen[0][0] if seen else ""
    # 期望值**独立地**拼一遍（不抄 `_feed_waiting` 内部那个表达式），
    # 断的是"投递确实走的是 render_briefing"这条不变量
    delivered = {**state["tasks"][0]}
    delivered["qa"] = [
        *delivered["qa"],
        {"by": "用户", "q": delivered.get("asked") or "", "a": "篇幅一页"},
    ]
    expect = render_briefing(delivered)
    check("提示词 == render_briefing(投递后的 Task)", prompt == expect, f"实际：\n{prompt[:400]}")
    check("它上次问过什么也带上了", "请问用在哪" in prompt)
    # 🔴 **回归点**（2026-11-04 日志定位）：【问答经过】的「问」= `ask_user` 的参数原话。
    #    取 `report` 的话，这里换成那段过场 —— 用户看到的是"答：A"，却不知道 A 在答什么。
    check("「问」取的是 `ask_user` 的参数原话，**不是**它最后说的那段",
          "这份导学单用在哪？（课前预习 / 课中探究 / 课后巩固）" in prompt,
          prompt[-400:])
    check("那段过场仍在（作为「它最后说的那句」），没被丢掉",
          "我先设计了目标" in prompt, prompt[-400:])

    print("\n⑤ 用户原话跟着 Task 走（快照在 Task 上 → 这条重入路径**自动**带上）")
    seen = _capture()
    state["tasks"][0]["user_turns"] = [
        {"n": 1, "text": "帮我备一节三年级语文《荷花》的课，第一课时，直接生成不用再问我。"},
    ]
    _run_feed(state)
    prompt, turns = seen[0] if seen else ("", None)
    check("清单进了交接书", "第 1 轮" in prompt and "直接生成不用再问我" in prompt, prompt[-260:])
    # 🔴 交接书里只有清单，**全文走 `read_user_turn`** —— 所以原话必须一路传到 run_task
    check("原话也传给了执行器（read_user_turn 要用）", bool(turns), "" if turns else "传了个空的")

    print("\n⑥ 重入**什么都没交出来** → 上一轮交付的产物不许被抹掉")
    # 🔴 2026-11-03 审核实测到的真 bug：settle 那行原来写的是
    #    `rec.update(stage="completed", artifacts=outcome.artifacts)` —— **无条件覆盖**，
    #    把 `absorb_outcome` 的保护（"这一轮没产出，不能把上一轮的抹掉"）当场抵消。
    #    后果：台账 / 终端任务表 / `_run.md` / 断言 `every_task_has_artifact`
    #    看到的都是"这个任务没交付过东西"。同一形状在 failed 分支上踩过一次
    #    （见 `checks.check_every_task_has_artifact` 的 docstring）。
    # ⚠️ 2026-11-05：这条原来喂的是 `completed` + 空产物 —— 判定退休后**那个状态不可达**
    #    （三态由结构定，`artifacts` 空就不可能是 `completed`），等于在测一条走不通的路。
    #    换成真正可达的 `incomplete`（推满轮次）：**要守的不变量一个字没变**。
    proto.executor.run_task = _stub_returns(_no_delivery())  # type: ignore[assignment]
    state6 = {
        "messages": [HumanMessage("就按这个改")],
        # 🔴 **标识**（`solution.md` §0.65 Y1）：这句在回答哪个待答任务 ——
        #    没有它**一条都不投**（那是主 agent 的输入）。值 = 下面那个待答 Task 的 id。
        "answer_to": "t1",
        "tasks": [{
            "id": "t1", "skill": "guided-learning", "task": "生成导学单",
            "stage": "waiting_for_input", "artifacts": ["art_001"],   # ← 上一轮交付的
            "refs": [], "qa": [{"by": "用户", "q": "用在哪？", "a": "课前预习"}],
            "report": "请问用在哪？",
        }],
    }
    out6, _ = _run_feed(state6)
    check("stage 迁移到 failed（没交付就是没交付）", out6[0]["stage"] == "failed", out6[0]["stage"])
    check("**上一轮交付的产物还在**（没被这一轮的空 artifacts 抹掉）",
          out6[0]["artifacts"] == ["art_001"], repr(out6[0]["artifacts"]))

    print("\n⑥' 投递那趟遇到**平台故障**：故障要落进 Task（台账才说得出真话）")
    # 🔴 **2026-11-05 修 A3**：故障原来只活在 `ExecOutcome` 上，落到 Task 就只剩 `stage=failed`
    #    → 台账说"❌ 它没交付、也没说要什么"（＝执行器没干成）—— 而回执说"⚠️ 平台故障，
    #    原样再派"。**同一件事两副面孔**，主 agent 做下一轮决定看的是台账。
    #    ⚠️ 投递这条路**没有回执**（回执只属于 `delegate` 工具调用）—— 所以这里台账是
    #    **唯一的信号**，说错了没有任何东西能纠正它。
    from proto.handoff.ledger import render_ledger as _ledger

    proto.executor.run_task = _stub_returns(ExecOutcome(  # type: ignore[assignment]
        status="incomplete", artifacts=[], report="", rounds=1,
        fault="TimeoutError: （模拟）投递那趟抖动"))
    state6b = {
        "messages": [HumanMessage("就按这个改")],
        # 🔴 **标识**（`solution.md` §0.65 Y1）：这句在回答哪个待答任务 ——
        #    没有它**一条都不投**（那是主 agent 的输入）。值 = 下面那个待答 Task 的 id。
        "answer_to": "t1",
        "tasks": [{
            "id": "t1", "skill": "guided-learning", "task": "生成导学单",
            "stage": "waiting_for_input", "artifacts": [], "refs": [],
            "report": "请问用在哪？", "qa": [],
        }],
    }
    out6b, _ = _run_feed(state6b)
    check("故障这一趟：stage 记 failed", out6b[0]["stage"] == "failed", out6b[0]["stage"])
    check("**故障原因落进了 Task**（不落 → 台账只能说「没交付」这句假话）",
          out6b[0].get("fault") == "TimeoutError: （模拟）投递那趟抖动",
          repr(out6b[0].get("fault")))
    _led6b = _ledger(out6b)
    check("台账说的是「平台故障」，不是「没交付」",
          "平台故障" in _led6b and "没交付" not in _led6b, _led6b)
    check("台账**不打**「执行器说」（它一个字都没说）", "执行器说" not in _led6b, _led6b)

    print("\n⑦ 投递重跑那轮**没有回执** —— 台账不许再说「全文见回执」（2026-11-04 复审 E2）")
    # 🔴 投递是**平台**直接调 `run_task`（不经过 `delegate`）→ 主 agent 的上下文里
    #    **没有**这一轮的回执。而台账原来对两条路都写"全文见那次的回执"——
    #    主 agent 翻过去只会看到**上一轮**的回执（内容还是旧的），把旧的当成当前的。
    #    ⚠️ 这条特意让上一轮是 `delegate` 起的（`run_by="delegate"`）：那样上下文里
    #    **确实有一份回执**（正是最容易被误认的那种情形）。
    from proto.handoff.ledger import render_ledger

    # > 150 字，逼出台账的截断
    # ⚠️ **长的那一份要放在 `asked` 上**（2026-11-04）：台账 waiting 那档现在打的是
    #    `asked`（"它要的信息"），不是 `report`（那段过场）—— 见 `ledger.render_ledger`。
    proto.executor.run_task = _stub_returns(_waiting(  # type: ignore[assignment]
        "问课时" * 60, asked="用几个课时？" * 30))
    state7 = {
        "messages": [HumanMessage("两课时")],
        # 🔴 **标识**（`solution.md` §0.65 Y1）：这句在回答哪个待答任务 ——
        #    没有它**一条都不投**（那是主 agent 的输入）。值 = 下面那个待答 Task 的 id。
        "answer_to": "t1",
        "tasks": [{
            "id": "t1", "skill": "quiz", "task": "出随堂小测",
            "stage": "waiting_for_input", "artifacts": [], "refs": [],
            "report": "问课时",
            "qa": [], "user_turns": [], "run_by": "delegate",   # ← 上一轮是主 agent 派的
        }],
    }
    out7, _ = _run_feed(state7)
    check("这一轮记成了 `feed`", out7[0].get("run_by") == "feed", repr(out7[0].get("run_by")))
    led = render_ledger(out7)
    # 🔴 **2026-11-06**：待答那档**不再重印问题**（`ledger`〔沿革 11〕）—— 全文出处是
    #    【待答】（`node_finalize` **无条件**注入、且是全的）。所以原来那三条
    #    （不指向回执 / 说清"不在你上下文里" / 切了多少字照标）**在这一档不适用了**：
    #    它们断的是 `brief` 那套截断，随那行一起去掉。
    #    ⚠️ **截断 ＋ "全文在哪由 `run_by` 决定"** 仍由 `unit_handoff` ②′ 守着
    #    （`failed` 那档的 `report`，含 `delegate` / `feed` / 缺 `run_by` 三条）。
    check("台账**不再**指向回执（那一轮根本没有回执）",
          "全文见" not in led, repr(led[-120:]))
    check("台账待答那档：**不重印问题**，只说它多大 ＋ 已由平台转达",
          "用几个课时？" not in led and "平台已原样转达" in led, repr(led[-160:]))

    print("\n⑧ E1：投递重跑后**还是缺信息** → 也先让主 agent 试答（两条路统一）")
    # 🔴 用户 2026-11-04：*"肯定得统一啊 都先走试答啊"*。
    #    原来投递那条路直接又登记待答 —— 用户刚答完一轮就**又被问一遍**，
    #    而主 agent 这时**已经知道用户刚说的那句**，很可能答得上。
    #    ⚠️ 这条守的是"两条路走同一个函数"（`agent.try_answer_then_rerun`）：
    #    拿掉投递路径那一步 → 这里红。
    seen8 = _capture()
    proto.agent._ask_main = _stub_returns("课时按两课时算")  # type: ignore[assignment]
    try:
        state8 = {
            "messages": [HumanMessage("两课时")],
            # 🔴 **标识**（`solution.md` §0.65 Y1）：这句在回答哪个待答任务 ——
            #    没有它**一条都不投**（那是主 agent 的输入）。值 = 下面那个待答 Task 的 id。
            "answer_to": "t1",
            "tasks": [{
                "id": "t1", "skill": "quiz", "task": "出随堂小测",
                "stage": "waiting_for_input", "artifacts": [], "refs": [],
                "report": "用几个课时？",
                "qa": [], "user_turns": [],
            }],
        }
        out8, logs8 = _run_feed(state8)
    finally:
        proto.agent._ask_main = _stub_returns("")  # type: ignore[assignment]
    check("试答之后**又重跑了一次**执行器（run_task 被调 2 次）", len(seen8) == 2, f"{len(seen8)} 次")
    # 🔴 **「问」= `outcome.asked`**（`ask_user` 的参数原话），**不是** `outcome.report`
    #    （它最后说的那段话）—— 2026-11-04 日志定位。两条一起断，缺一条就分不出取错了源。
    check("试答**成对记进 `qa`**（`by=主 agent`；`q` 是**它申报要的那句原话**）",
          any(x.get("by") == "主 agent" and x.get("q") == WAITING_ASKED
              for x in out8[0]["qa"]),
          repr(out8[0]["qa"]))
    check("「问」**不是**那段过场（`report`）—— 取错源的话这条红",
          all(x.get("q") != WAITING_REPORT
              for x in out8[0]["qa"] if x.get("by") == "主 agent"),
          repr(out8[0]["qa"]))
    check("日志里说破了这一跳（不然读的人以为平台凭空多跑了一轮）",
          any("试答" in ln for ln in logs8), repr(logs8))

    print("\n⑨ 平台动作是**对象**，断言**读对象**（CLAUDE.md 那条已知违反的收尾）")
    # 🔴 2026-11-04：`checks.py` / `harness._turn_summary` 原来在 `state["log"]` 的**渲染串**上
    #    做子串匹配（`"finalize: 透传" in ln`）—— 改一句文案，断言就**静默给出错误结论**
    #    （判据一：渲染产物不许被反解析回结构）。现在平台在**发生那一刻**写 `actions`
    #    （对象），断言读它；`log` 那句人话仍在同一个调用点写（给人看 / `_run.md`）。
    seen9 = _capture()
    state9 = {
        "messages": [HumanMessage("两课时")],
        # 🔴 **标识**（`solution.md` §0.65 Y1）：这句在回答哪个待答任务 ——
        #    没有它**一条都不投**（那是主 agent 的输入）。值 = 下面那个待答 Task 的 id。
        "answer_to": "t1",
        "tasks": [{
            "id": "t1", "skill": "quiz", "task": "出随堂小测",
            "stage": "waiting_for_input", "artifacts": [], "refs": [],
            "report": "用几个课时？",
            "qa": [], "user_turns": [],
        }],
    }
    out9 = _run_node_feed(state9)
    acts9 = out9.get("actions") or []
    check("投递那一跳写了**结构化动作**（`did=feed`）",
          [a.get("did") for a in acts9] == ["feed"], repr(acts9))
    check("动作里带**投给了谁**（Task id，不是从句子尾巴切的）",
          acts9 and acts9[0].get("tasks") == ["t1"], repr(acts9))
    check("动作里带**多少字**（用户原话长度）",
          acts9 and acts9[0].get("chars") == len("两课时"), repr(acts9))
    check("同一刻那句**人话仍在** `log` 里（给人看的，没被删）",
          any("定向投递" in ln for ln in (out9.get("log") or [])), repr(out9.get("log")))

    print("\n⑩ 定向投递（`solution.md` §0.65 Y1）：**标识由通道填，平台不猜**")
    # 🔴 2026-11-05：原来是**广播**（把用户这句话投给**所有**待答）—— 一句话只答了一个，
    #    其余全都要白跑一趟（各 60–120 秒，实测 3.8% 的轮次会碰上）。
    #    现在：**有标识 → 只投那一个；没标识 → 一条都不投**（那是主 agent 的输入）。
    #    ⚠️ 平台**不判断**"这句在答谁"（语义判断，F7 的死因）—— 标识来自**通道**。
    _two = {
        "messages": [HumanMessage("答第一条的")],
        "answer_to": "tA",
        "tasks": [
            {"id": "tA", "skill": "quiz", "task": "出小测", "stage": "waiting_for_input",
             "artifacts": [], "refs": [], "report": "难度？", "qa": [], "user_turns": []},
            {"id": "tB", "skill": "lesson-prep", "task": "备课", "stage": "waiting_for_input",
             "artifacts": [], "refs": [], "report": "课型？", "qa": [], "user_turns": []},
        ],
    }
    _seen = _capture()
    _out, _ = _run_feed({**_two})
    check("**只投标识指的那一条**（另一条不动）",
          len(_seen) == 1 and [t["id"] for t in _out] == ["tA"],
          f"投了 {len(_seen)} 次：{[t['id'] for t in _out]}")
    check("**没被投的那条**原样留着（还在等，没被碰）",
          any(t["id"] == "tB" for t in _two["tasks"]) and not any(t["id"] == "tB" for t in _out),
          repr([t["id"] for t in _out]))
    _seen2 = _capture()
    _out2, _ = _run_feed({"messages": [HumanMessage("随便说点什么")],
                                    "tasks": _two["tasks"]})
    check("**没有标识 → 一条都不投**（那是主 agent 的输入）",
          not _seen2 and not _out2, f"投了 {len(_seen2)} 次")
    _seen3 = _capture()
    _out3, _ = _run_feed({**_two, "answer_to": "tZZZ"})
    check("标识指向**不存在的任务** → 也不投（不猜、不广播）",
          not _seen3 and not _out3, f"投了 {len(_seen3)} 次")

    # 守卫：断言层不许再回头去解析 `log`（AST 扫字符串常量，不看注释）
    def _log_refs(path: str) -> list[str]:
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        out: list[str] = []
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(isinstance(n, ast.Constant) and n.value == "log" for n in ast.walk(fn)):
                out.append(fn.name)
        return sorted(set(out))

    check("`checks.py` 里**一次都不许**引用 `log`（断言只读对象）",
          not _log_refs("evals/checks.py"), repr(_log_refs("evals/checks.py")))
    check("`harness.py` 里 `log` 只许出现在「快照复制 / `_run.md` 渲染」两处",
          _log_refs("evals/harness.py") == ["dump_run_record", "snapshot"],
          repr(_log_refs("evals/harness.py")))

    # 🔴 断言**不许在非结构化文本里找词**（用户 2026-11-05：*"不准在任何非结构化的文本里
    # 通过关键字或者正则匹配的方式来做强逻辑判断 —— 绝对禁止"*）。
    # 硬断言只许断言**平台自己的事实**（任务状态 / 平台动作 / 产物账 / 谁写的）；
    # 模型吐出来的东西（产物正文、回复、判定写的字段）→ 要么走 `judge.py` 的软 rubric
    # （明说是概率判断），要么不断 —— **找词那种，过了不代表对、挂了不代表错**。
    # ⚠️ 扫的是 **AST**（真的调用），不是源码文本 —— 注释和 docstring 里为了说明"原来错在哪"
    #    会引用旧写法，那不是代码（踩过一次：第一版按文本扫，被自己注释里的旧写法判红 ✗）。
    tree = ast.parse(io.open("evals/checks.py", encoding="utf-8").read())
    attrs = [
        f"{n.func.value.id}.{n.func.attr}"
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
    ]
    check("`checks.py` 里没有正则（`re.*`）", not [a for a in attrs if a.startswith("re.")], repr(attrs))
    bad_calls = [a for a in attrs if a.split(".")[-1] in ("count", "partition", "split", "startswith")]
    check("`checks.py` 里没有对文本 count / partition / split / startswith", not bad_calls, repr(bad_calls))
    # 子串匹配的形状：**字符串字面量在左、`in` 在右**（`"xxx" in ln`）——
    # 容器成员判断（`aid in arts`）左边是变量，不在此列 ✓
    substr = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Compare)
        and any(isinstance(op, (ast.In, ast.NotIn)) for op in n.ops)
        and isinstance(n.left, ast.Constant)
        and isinstance(n.left.value, str)
    ]
    check("`checks.py` 里没有「字符串字面量 in 文本」", not substr, f"第 {substr} 行")

    # 🔴 **断言不许在"模型说的话"里找内容**（2026-11-05；用户原话：*"评测层那几个根据
    #    字符匹配的断言本质上没任何效果 都是自欺欺人 …… 你是在断言 llm 吐出来的东西啊"*）。
    #    判据是**形状**、不是名字（改名也逃不掉）：一个函数里**既读 `turn["reply"]`、
    #    又做 `in` 子串比较** —— 那就是在拿模型的话当判据 ✗（`reply_contains` /
    #    `reply_excludes` 就是这么被删掉的）。真要判语义 → 走软 rubric（判断留档）。
    _reply_checks: list[str] = []
    for _fn in ast.walk(ast.parse(io.open("evals/checks.py", encoding="utf-8").read())):
        if not isinstance(_fn, (ast.FunctionDef, ast.AsyncFunctionDef)):   # `async def` 是另一个类
            continue
        # ⚠️ **两种写法都要抓**（第一版只认下标 `turn["reply"]`，而代码里用的是
        #    `turn.get("reply")` —— 探针当场证明那条守卫**不挡人** ✗）。
        #    所以判据是"函数里出现了 `"reply"` 这个常量"（`.get("reply")` / `["reply"]` 都中）。
        _reads = any(
            isinstance(n, ast.Constant) and n.value == "reply" for n in ast.walk(_fn)
        )
        # ⚠️ **`in` 和 `not in` 都要认**（第二版只认 `In`，而 `s not in r` 是 `NotIn` ✗
        #    —— 又是探针抓出来的）。
        _has_in = any(
            isinstance(n, ast.Compare)
            and any(isinstance(op, (ast.In, ast.NotIn)) for op in n.ops)
            for n in ast.walk(_fn)
        )
        if _reads and _has_in:
            _reply_checks.append(_fn.name)
    check("`checks.py` 里没有「读 reply ＋ 子串判断」的断言（模型的话不是判据）",
          not _reply_checks, repr(_reply_checks))

    # 闭环：那三条断言**真的读得到**动作（合成一份 run record，纯函数、秒级）
    from evals.checks import CHECKS

    def _run(actions: list[dict]) -> dict:
        turn = {"turn": 1, "actions": actions, "log": ["（这里故意不写那句人话）"]}
        return {"turns": [turn]}

    relayed = [{"by": "platform.finalize", "did": "relay", "tasks": ["t1"], "chars": 10}]
    fed = [{"by": "platform.prepare", "did": "feed", "tasks": ["t1"], "chars": 3}]
    t1 = {"turn": 1}
    check("`platform_relayed`：有 relay 动作 → 过（**不看 log**）",
          CHECKS["platform_relayed"](_run(relayed), t1)[0] is True)
    check("`platform_relayed`：没有 → 不过（原来靠日志串，现在是靠对象）",
          CHECKS["platform_relayed"](_run(fed), t1)[0] is False)
    check("`no_platform_relay`：没有 relay → 过", CHECKS["no_platform_relay"](_run(fed), t1)[0] is True)
    check("`platform_fed_answer`：有 feed 动作 → 过（**不看 log**）",
          CHECKS["platform_fed_answer"](_run(fed), t1)[0] is True)

    print("\n⑪ 投递重跑完成 → 更早的同技能待答**显式取代**（哑待答，2026-11-06）")
    # 🔴 实测：用户没走 `↩` 答复通道、又发了新消息，
    #    新任务交付后旧待答永远挂着（每轮重摆、答它=重跑旧任务）。
    #    规则与边界见 `briefing.moot_older_waiting` —— 这里守的是**投递那条路的接线**：
    #    完成时它真的被调用、结果真的进了返回的 tasks（reducer 才合得上）。
    turns1 = [{"n": 1, "text": "给我做个导学单", "after": ""}]
    turns2 = [*turns1, {"n": 2, "text": "用在课前预习", "after": ""}]
    proto.executor.run_task = _stub_returns(ExecOutcome(  # type: ignore[assignment]
        status="completed", artifacts=["art_ok"], report="（交付了）", rounds=1))
    st = {
        "messages": [HumanMessage("用在课前预习")],
        "answer_to": "t-new",
        "tasks": [
            # ⚠️ t-old 的 `run_by` 故意留 "feed"（真实形状：它上一轮被投递过、又问了）——
            #    取代的副本带着它混回 `updated` 时，"投给了谁"不许被它污染。
            {"id": "t-old", "skill": "guided-learning", "stage": "waiting_for_input",
             "artifacts": [], "refs": [], "qa": [], "run_by": "feed", "user_turns": turns1},
            {"id": "t-new", "skill": "guided-learning", "stage": "waiting_for_input",
             "artifacts": [], "refs": [], "qa": [], "user_turns": turns2},
        ],
    }
    out, _logs = _run_feed(st)
    by_id = {t["id"]: t for t in out}
    check("被投递的那条落成 completed", by_id.get("t-new", {}).get("stage") == "completed",
          repr(by_id.get("t-new")))
    check("更早的同技能待答被**显式取代**（进了返回的 tasks，reducer 才合得上）",
          by_id.get("t-old", {}).get("stage") == "superseded", repr(by_id.get("t-old")))
    # 🔴 取代副本不许污染"这轮投给了谁"（`node_feed` 的 actions 读它）——
    #    t-old 的 `run_by` 是上一轮留下的 "feed"，只认 run_by 会把它报成"投过"。
    pre = _run_node_feed({**st, "log": []})
    fed_ids = [i for a in (pre.get("actions") or []) if a.get("did") == "feed"
               for i in a.get("tasks", [])]
    check("actions 里「投给了谁」只有被投的那条（取代副本不许混进来）",
          fed_ids == ["t-new"], repr(pre.get("actions")))
    # 🔴 **反例两条**（判据的边界，外审 N3 点的就是这里 —— 原来只测了"延长"那一条）：
    #    说不清就不动。① 快照**没延长**（同一轮里重派 = F7 场景）；② **没有快照**。
    from proto.handoff.briefing import moot_older_waiting

    _same = {"id": "t-same", "skill": "guided-learning", "stage": "waiting_for_input",
             "artifacts": [], "refs": [], "qa": [], "user_turns": turns2}
    check("快照**没延长**（同一轮里重派）→ 一个字不碰（F7 场景：主 agent 自己编答案重派）",
          moot_older_waiting({"id": "t-done", "skill": "guided-learning",
                              "user_turns": turns2}, [_same]) == [])
    _no_snap = {"id": "t-nosnap", "skill": "guided-learning", "stage": "waiting_for_input",
                "artifacts": [], "refs": [], "qa": []}
    check("**没有快照**（fixture / 手写 state）→ 一个字不碰（说不清就不动）",
          moot_older_waiting({"id": "t-done", "skill": "guided-learning",
                              "user_turns": turns2}, [_no_snap]) == [])
    # 🔴 **接线守卫**（AST，不看注释）：两条路（delegate / feed）都必须**真的走到**它 ——
    #    少接一条，那条路上的哑待答就静默漏掉（delegate 那条没有 e2e 单测，只能这样钉）。
    # 🔴 **2026-11-06 改（外审 F5 收口）**：调用点从"两条路各写一次"收进 `agent.dispatch`
    #    （两条路都走它，见它的 docstring）—— 所以这里断的是"**收在一处**"，
    #    "那两条路真的调了 dispatch"由 `unit_stop_on_pending` ⑧ 补上（两段合起来才是覆盖面）。
    _moot_sites: set[tuple[str, str]] = set()
    for _f in ("proto/agent.py", "proto/graph.py"):
        _tree = ast.parse(io.open(_f, encoding="utf-8").read())
        for _fn in [n for n in ast.walk(_tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "moot_older_waiting" for n in ast.walk(_fn)):
                _moot_sites.add((_f, _fn.name))
    check("`moot_older_waiting` **收在一处**（`agent.dispatch` —— 两条路都走它）",
          _moot_sites == {("proto/agent.py", "dispatch")}, repr(sorted(_moot_sites)))

    print("\n⑫ `node_feed` 必须**只有一条返回路径**，且它写回 `answer_to=''`（外审 N1）")
    # 🔴 实测（2026-11-06，外审 N1）：
    #    `State.answer_to` 的注释**声称**"每条返回路径都写回 ''"—— 而主返回路径（有 tasks
    #    那条）漏了。后果不是"少写一个键"：`answer_to` 跟着 checkpointer 活到下一轮，
    #    于是**在 `↩` 下答过一次之后**，用户回车跳过待答、回 `👤` 说的**任何新消息**
    #    都会被 `_feed_waiting` 当成"在回答那条"投给旧任务 —— 每次白跑一趟执行器
    #    （60–120s）＋ 答非所问，而且**每一轮都这样**。
    #    ⚠️ 单测原来抓不到：`_feed_waiting` 的用例都**直接喂** `answer_to`（那是它的**输入**），
    #    没人看**节点返回的 dict 有哪些键**。同类信号 `fed_asking` 有守卫
    #    （`unit_stop_on_pending` ⑦ 盯两个 schema），`answer_to` 一个都没有 —— 补上。
    #
    # 🔴 **2026-11-06 晚：根治的形状变了**（外审 F4 拆分之后）。原来靠"**每条**返回路径
    #    都要写"这条纪律 + 守卫盯着每一条；现在投递**只有一条返回路径**了 ——
    #    **没有第二条可漏**。所以守卫也跟着改成断**条数**（多一条就红）：
    #    纪律守不住的地方，靠形状守住。
    _tree = ast.parse(io.open("proto/graph.py", encoding="utf-8").read())
    _fn = next(n for n in ast.walk(_tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "node_feed")
    _returns = [n for n in ast.walk(_fn) if isinstance(n, ast.Return)]
    _keys: set[str] = set()
    _bad: list[str] = []
    for _r in _returns:
        if not isinstance(_r.value, ast.Dict):     # 返回值不是字面量 dict → 守卫看不见它
            _bad.append(f"第 {_r.lineno} 行：返回值不是字面量 dict（守卫够不着 → 请改守卫）")
            continue
        _keys |= {k.value for k in _r.value.keys
                  if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    _missing = {"answer_to", "fed_asking"} - _keys
    check("`node_feed` **只有一条** `return`（多一条 = 又有了「漏写哪条」的机会）",
          len(_returns) == 1, f"{len(_returns)} 条")
    check("它的返回值写回 `answer_to=''` ＋ `fed_asking`（本轮信号，本轮写）",
          not _bad and not _missing, repr(_bad or sorted(_missing)))

    # 再按**今天这条真实路径**跑一遍（AST 管"有没有"，这里管"值对不对"）
    _done = {"id": "t1", "skill": "quiz", "stage": "completed",
             "artifacts": [], "refs": [], "qa": []}
    _ret = _run_node_feed({"messages": [HumanMessage("顺便再出三道题")],
                            "tasks": [_done], "answer_to": "t1", "log": []})
    check("投递后 `answer_to` 清成空串 ＋ `fed_asking` 写成本轮值",
          _ret.get("answer_to") == "" and "fed_asking" in _ret,
          repr({k: _ret.get(k) for k in ("answer_to", "fed_asking")}))
    # 台账那半：`prepare` 不再碰这两个键（它们跟着投递走了）—— 免得两处都写又漂开
    _prep = _run_node_prepare({"messages": [HumanMessage("你好")], "tasks": [_done]})
    check("`node_prepare` **不碰** `answer_to` / `fed_asking`（它们归 `feed`，一处写）",
          "answer_to" not in _prep and "fed_asking" not in _prep, repr(sorted(_prep)))

    print("\n⑬ 🔴 `moot_older_waiting` 的判据②建在一个**前提**上：快照只增不改")
    # 外审 N3（【判断】）：判据②用"列表相等"做**前缀判定**（`done_turns[:len(t_turns)] == t_turns`），
    # 它成立的前提是 **`user_turns` 快照 append-only** —— 谁把它改成"按需重算"（比如只留最近 N 轮、
    # 或渲染时现取），前缀判定就**静默失效**（永远判"没延长" → 哑待答再也不被取代）。
    # ⇒ 把前提本身钉住（**性质测试**，不是三个 case）：消息只往后长，快照必须前缀稳定。
    from langchain_core.messages import AIMessage

    from proto.agent import _user_turns

    _msgs = [
        HumanMessage("第一句"),
        AIMessage("主 agent 的回话"),
        HumanMessage("第二句"),
        AIMessage("平台透传的那条（也进快照的 after）"),
        HumanMessage("第三句"),
    ]
    _stable = all(_user_turns(_msgs)[: len(_user_turns(_msgs[: k]))] == _user_turns(_msgs[: k])
                  for k in range(1, len(_msgs) + 1))
    check("`_user_turns` **前缀稳定**（消息只增 → 旧轮次的快照一字不变）", _stable,
          repr([t["n"] for t in _user_turns(_msgs)]))
    # 另一半前提：快照**存在 Task 上**（不是渲染时现取）—— 投递那条路才会自动带着它。
    _rec_keys = [n.value for n in ast.walk(ast.parse(
        io.open("proto/agent.py", encoding="utf-8").read()))
        if isinstance(n, ast.Constant) and n.value == "user_turns"]
    check("`delegate` 把快照**存进 Task**（键在 `rec` 字面量里 —— 现取就没有 append-only 可言）",
          bool(_rec_keys))

    print("\n⑭ 默认执行路径是 `auto`（产品行为：两条路都开）")
    # 外审 N5 那一小项。⚠️ 真正靠这个默认值的**只有 `python -m proto`** ——
    # `evals.chat` 与 `evals.harness` 都按 case 显式设（harness 每次 `or "auto"` 重置）。
    # 所以这里守的是"**别把它写死成别的值忘了改回**"（比如为了验 delegate 改成 "delegate"）。
    check("`proto.agent.EXEC_PATH` 默认是 `auto`", proto.agent.EXEC_PATH == "auto",
          f"当前 = {proto.agent.EXEC_PATH!r}")

    print("\n⑮ 每个单测模块都把日志**指向 `tmp/logs/`**（`logs/` 只留真会话）")
    # 外审 N4 那条的延伸，＋ 2026-11-06 用户定的分工：
    # **`logs/` 是给人看"真会话"的地方**，测试 / 跑批的日志是**噪音** ——
    # 实测 `logs/` 2309 个文件里 **826 个是噪音**（670 单测 ＋ 148 空 ＋ 8 探针），
    # 把翻会话日志的路堵死了。⇒ 单测设 `PROTO_LOG_DIR=tmp/logs`（`tmp/` 在 gitignore 里）。
    # ⚠️ `PROTO_LOG_TAG` **仍然要设**：多个模块同秒同跑时，`tmp/logs/` 里也要分得开。
    # ⚠️ **必须是机制**：新加一个单测模块忘了设 → 它的日志就混进真会话里，
    #    而且**没有任何地方会红**（这次就是靠这条守卫把它钉住的）。
    import glob as _glob
    import re as _re

    #: ⚠️ **豁免**：`unit_frozen_paths` 测的就是**默认路径**（冻结后落在 exe 旁边），
    #:    设了 `PROTO_LOG_DIR` 就把它顶掉了 —— 而它自己写进 `fake/logs`（临时目录，
    #:    随测试 `rmtree`），本来就不污染 `logs/`。
    _no_dir_exempt = {"evals/unit_frozen_paths.py"}

    _untagged: list[str] = []
    for _f in sorted(_glob.glob("evals/unit_*.py")):
        _src = io.open(_f, encoding="utf-8").read()
        # 设了 tag ＋ 文件名带 `unit-`（值写错成别的，等于没打）
        if 'os.environ["PROTO_LOG_TAG"] = "unit-' not in _src:
            _untagged.append(_f)
        # ⚠️ `glob` 在 Windows 上给的是反斜杠 —— 比之前**归一化**，否则豁免形同虚设
        #    （实测：`evals\unit_frozen_paths.py` 对不上 `evals/unit_frozen_paths.py`）。
        if ('os.environ["PROTO_LOG_DIR"]' not in _src
                and _f.replace(os.sep, "/") not in _no_dir_exempt):
            _untagged.append(f"{_f}（没设 `PROTO_LOG_DIR`）")
    check("每个 `evals/unit_*.py` 都设了 `PROTO_LOG_TAG=\"unit-…\"` ＋ `PROTO_LOG_DIR`",
          not _untagged, repr(_untagged))
    # ⚠️ 还有一半：**必须排在所有非标准库 import 之前**（`trace` 在 import 那一刻读 env
    #    定文件名）。⚠️ 只查"在 `import proto` 之前"**不够** —— 实测踩过：`unit_decision`
    #    的 tag 原来设在 `from evals import chat` 之后，而**那一行就把 `proto.trace`
    #    间接导进来了** → 日志照样没 tag，而"直接 proto import"那版守卫**放它过去了**。
    #    ⇒ 判据改成：赋值行必须早于**第一个非标准库 import**（`sys.stdlib_module_names`）。
    import sys as _sys

    _late: list[str] = []
    for _f in sorted(_glob.glob("evals/unit_*.py")):
        _tree = ast.parse(io.open(_f, encoding="utf-8").read())
        _set_at = [n.lineno for n in ast.walk(_tree)
                   if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Subscript) for t in n.targets)
                   and ("PROTO_LOG_TAG" in ast.dump(n)
                        or "PROTO_LOG_DIR" in ast.dump(n))]
        _imp_at = []
        for _n in ast.walk(_tree):
            if isinstance(_n, ast.Import):
                _mods = [a.name.split(".")[0] for a in _n.names]
            elif isinstance(_n, ast.ImportFrom):
                _mods = [( _n.module or "").split(".")[0]]
            else:
                continue
            if any(m and m not in _sys.stdlib_module_names and m != "__future__"
                   for m in _mods):
                _imp_at.append(_n.lineno)
        if not _set_at or (_imp_at and min(_set_at) > min(_imp_at)):
            _late.append(_f)
    check("而且**排在第一个非标准库 import 之前**（间接 import 也会拉进 `proto.trace`）",
          not _late, repr(_late))

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
