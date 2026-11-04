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
import io
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from langchain_core.messages import HumanMessage

import proto.executor
from proto import graph

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail else ""))


class _Waiting:
    """假的 ExecOutcome：投递后仍待答（不关心内容，只看提示词）。"""

    status = "waiting_for_input"
    artifacts: list[str] = []
    report = "（还需要信息）"
    progress = ""
    missing: list[str] = []


class _CompletedNoArtifact:
    """假的 ExecOutcome：**完成了，但这一轮没写新产物**（成果在回复里）—— 见 ⑥。"""

    status = "completed"
    artifacts: list[str] = []
    report = "按你说的改完了"
    progress = ""
    missing: list[str] = []


def _capture() -> list[tuple[str, list | None]]:
    """把 `run_task` 换成记录器，返回它每次收到的 **（提示词, 用户原话）**。

    ⚠️ `run_task` 是**模块属性**调用（`executor.run_task(...)`）—— 两条路都是，
    所以改这一处就够（原来是值导入，得两处各改一次）。
    """
    seen: list[tuple[str, list | None]] = []

    def fake(skill, briefing: str = "", user_turns=None):  # noqa: ANN001, ANN202
        seen.append((briefing, user_turns))
        return _Waiting()

    proto.executor.run_task = fake  # type: ignore[assignment]
    # 🔴 **试答那一步也要打桩**（2026-11-04 E1 之后，投递路径也会走它）：
    #    不打桩的话，每个"仍待答"的 section 都会**真调一次模型** ——
    #    **实测踩过**：忘了打桩 → 一个 section 多等 4.4 秒、还真花 token（单测必须不调 LLM）。
    #    要测"试答答得上"那条，自己在那一节里改成返回一句答案（见 ⑧）。
    proto.agent._ask_main = lambda *a, **k: ""  # type: ignore[assignment]
    return seen


def main() -> None:
    print("① 用户第二次答复时，第一次的答复必须还在提示词里")
    seen = _capture()
    state = {
        "messages": [HumanMessage("篇幅一页")],
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
    graph._feed_waiting(state)
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
    graph._feed_waiting(state)
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
    graph._feed_waiting(state)
    prompt = seen[0][0] if seen else ""
    check("可读产物进了交接书", readable in prompt and "《白桦》教案" in prompt, prompt[-260:])

    print("\n④ 投递的提示词**就是** `render_briefing(rec)`（三条路径共用同一份交接书）")
    from proto.handoff.briefing import render_briefing

    seen = _capture()
    state["tasks"][0].update(
        {
            "progress": "已完成学习目标",
            "missing": ["当堂检测"],
            "report": "我先设计了目标……请问用在哪？",
        }
    )
    graph._feed_waiting(state)
    prompt = seen[0][0] if seen else ""
    # 期望值**独立地**拼一遍（不抄 `_feed_waiting` 内部那个表达式），
    # 断的是"投递确实走的是 render_briefing"这条不变量
    delivered = {**state["tasks"][0]}
    delivered["qa"] = [
        *delivered["qa"],
        {"by": "用户", "q": delivered.get("report") or "", "a": "篇幅一页"},
    ]
    expect = render_briefing(delivered)
    check("提示词 == render_briefing(投递后的 Task)", prompt == expect, f"实际：\n{prompt[:400]}")
    check("它上次问过什么也带上了", "请问用在哪" in prompt)

    print("\n⑤ 用户原话跟着 Task 走（快照在 Task 上 → 这条重入路径**自动**带上）")
    seen = _capture()
    state["tasks"][0]["user_turns"] = [
        {"n": 1, "text": "帮我备一节三年级语文《荷花》的课，第一课时，直接生成不用再问我。"},
    ]
    graph._feed_waiting(state)
    prompt, turns = seen[0] if seen else ("", None)
    check("清单进了交接书", "第 1 轮" in prompt and "直接生成不用再问我" in prompt, prompt[-260:])
    # 🔴 交接书里只有清单，**全文走 `read_user_turn`** —— 所以原话必须一路传到 run_task
    check("原话也传给了执行器（read_user_turn 要用）", bool(turns), "" if turns else "传了个空的")

    print("\n⑥ 重入**完成**但这一轮没产出 → 上一轮交付的产物不许被抹掉")
    # 🔴 2026-11-03 审核实测到的真 bug：settle 那行原来写的是
    #    `rec.update(stage="completed", artifacts=outcome.artifacts)` —— **无条件覆盖**，
    #    把 `absorb_outcome` 的保护（"这一轮没产出，不能把上一轮的抹掉"）当场抵消。
    #    后果：台账 / 终端任务表 / `_run.md` / 断言 `every_task_has_artifact`
    #    看到的都是"这个任务没交付过东西"。同一形状在 failed 分支上踩过一次
    #    （见 `checks.check_every_task_has_artifact` 的 docstring）。
    proto.executor.run_task = lambda *a, **k: _CompletedNoArtifact()  # type: ignore[assignment]
    state6 = {
        "messages": [HumanMessage("就按这个改")],
        "tasks": [{
            "id": "t1", "skill": "guided-learning", "task": "生成导学单",
            "stage": "waiting_for_input", "artifacts": ["art_001"],   # ← 上一轮交付的
            "refs": [], "qa": [{"by": "用户", "q": "用在哪？", "a": "课前预习"}],
            "report": "请问用在哪？", "progress": "", "missing": [],
        }],
    }
    out6, _ = graph._feed_waiting(state6)
    check("stage 照常迁移到 completed", out6[0]["stage"] == "completed", out6[0]["stage"])
    check("**上一轮交付的产物还在**（没被这一轮的空 artifacts 抹掉）",
          out6[0]["artifacts"] == ["art_001"], repr(out6[0]["artifacts"]))

    print("\n⑦ 投递重跑那轮**没有回执** —— 台账不许再说「全文见回执」（2026-11-04 复审 E2）")
    # 🔴 投递是**平台**直接调 `run_task`（不经过 `delegate`）→ 主 agent 的上下文里
    #    **没有**这一轮的回执。而台账原来对两条路都写"全文见那次的回执"——
    #    主 agent 翻过去只会看到**上一轮**的回执（内容还是旧的），把旧的当成当前的。
    #    ⚠️ 这条特意让上一轮是 `delegate` 起的（`run_by="delegate"`）：那样上下文里
    #    **确实有一份回执**（正是最容易被误认的那种情形）。
    from proto.handoff.ledger import render_ledger

    class _LongWaiting(_Waiting):
        report = "问课时" * 60          # > 150 字，逼出台账的截断

    proto.executor.run_task = lambda *a, **k: _LongWaiting()  # type: ignore[assignment]
    state7 = {
        "messages": [HumanMessage("两课时")],
        "tasks": [{
            "id": "t1", "skill": "quiz", "task": "出随堂小测",
            "stage": "waiting_for_input", "artifacts": [], "refs": [],
            "report": "问课时", "progress": "", "missing": [],
            "qa": [], "user_turns": [], "run_by": "delegate",   # ← 上一轮是主 agent 派的
        }],
    }
    out7, _ = graph._feed_waiting(state7)
    check("这一轮记成了 `feed`", out7[0].get("run_by") == "feed", repr(out7[0].get("run_by")))
    led = render_ledger(out7)
    # ⚠️ 守的是**指针**（"全文见…"），不是"回执"这两个字 ——
    #    `_SRC["feed"]` 那句里**故意**写着"没有回执"（把"别去找"说破），它含"回执"二字是对的。
    check("台账**不再**指向回执（那一轮根本没有回执）",
          "全文见" not in led, repr(led[-120:]))
    check("台账说清了「全文不在你的上下文里」", "不在你的上下文里" in led, repr(led[-120:]))
    check("但**该标的还在**：切了多少字照标", "【平台截断" in led)

    print("\n⑧ E1：投递重跑后**还是缺信息** → 也先让主 agent 试答（两条路统一）")
    # 🔴 用户 2026-11-04：*"肯定得统一啊 都先走试答啊"*。
    #    原来投递那条路直接又登记待答 —— 用户刚答完一轮就**又被问一遍**，
    #    而主 agent 这时**已经知道用户刚说的那句**，很可能答得上。
    #    ⚠️ 这条守的是"两条路走同一个函数"（`agent.try_answer_then_rerun`）：
    #    拿掉投递路径那一步 → 这里红。
    seen8 = _capture()
    proto.agent._ask_main = lambda *a, **k: "课时按两课时算"  # type: ignore[assignment]
    try:
        state8 = {
            "messages": [HumanMessage("两课时")],
            "tasks": [{
                "id": "t1", "skill": "quiz", "task": "出随堂小测",
                "stage": "waiting_for_input", "artifacts": [], "refs": [],
                "report": "用几个课时？", "progress": "", "missing": [],
                "qa": [], "user_turns": [],
            }],
        }
        out8, logs8 = graph._feed_waiting(state8)
    finally:
        proto.agent._ask_main = lambda *a, **k: ""  # type: ignore[assignment]
    check("试答之后**又重跑了一次**执行器（run_task 被调 2 次）", len(seen8) == 2, f"{len(seen8)} 次")
    check("试答**成对记进 `qa`**（`by=主 agent`；`q` 是**那一刻它问的那句** —— 即 outcome 的 report）",
          any(x.get("by") == "主 agent" and x.get("q") == _Waiting.report
              for x in out8[0]["qa"]),
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
        "tasks": [{
            "id": "t1", "skill": "quiz", "task": "出随堂小测",
            "stage": "waiting_for_input", "artifacts": [], "refs": [],
            "report": "用几个课时？", "progress": "", "missing": [],
            "qa": [], "user_turns": [],
        }],
    }
    out9 = graph.node_prepare(state9)
    acts9 = out9.get("actions") or []
    check("投递那一跳写了**结构化动作**（`did=feed`）",
          [a.get("did") for a in acts9] == ["feed"], repr(acts9))
    check("动作里带**投给了谁**（Task id，不是从句子尾巴切的）",
          acts9 and acts9[0].get("tasks") == ["t1"], repr(acts9))
    check("动作里带**多少字**（用户原话长度）",
          acts9 and acts9[0].get("chars") == len("两课时"), repr(acts9))
    check("同一刻那句**人话仍在** `log` 里（给人看的，没被删）",
          any("投过去" in ln for ln in (out9.get("log") or [])), repr(out9.get("log")))

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

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
