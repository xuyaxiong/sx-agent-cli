"""单元级：**回执**（执行器 → 主 agent）要形式化、要完整（不调 LLM）。

    uv run python -m evals.unit_receipt

**为什么要有它**（2026-11-02 用户）：

> "交付内容跟委托任务交付的东西一样，**得形式化**呀，不要那么随意懂吗。
>  而且**不要截断** —— 我很关心 subagent 给主 agent 交付了什么呀，你截断了我怎么看。"

它是 `unit_briefing.py` 的**镜像**：那边管交接书（平台 → 执行器），这边管回执
（执行器 → 主 agent）。两边的规矩是同一条 —— **每个字段要么渲染、要么显式排除**。

> 有效性自检：把 `build_receipt` 里任意一段删掉，本测试必须红；
> 把 `report` 换成 `report[:150]`，② 必须红（2026-11-02 手工验过）。

🔴 **2026-11-03 起：记录层读的是「对象」，不是文本** —— 用户定的硬规矩：
*"禁止你反结构化任何 llm 正文输出 —— 结构化处理的前提一定是 llm 输出本身就是结构化的。"*
所以 ⑥ 断的是 **`build_receipt` → `Receipt` → `_receipt_sections`** 这条链，
**没有**任何"把渲染出来的文本解析回段"的代码（那条路已经整个删掉）。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import asyncio
import re
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
os.environ["PROTO_LOG_TAG"] = "unit-receipt"
os.environ["PROTO_LOG_DIR"] = "tmp/logs"   # 🔴 测试日志是**噪音** —— 别混进给人看的 `logs/`

from proto.handoff import BODY, SECTION
from proto.handoff.briefing import _flat
from proto.handoff.receipt import build_receipt, next_note, render_receipt
from proto import artifacts
from proto.artifacts import store_artifact
from proto.executor import ExecOutcome

#: **显式排除名单** —— 这些字段不进回执，每一条都要有理由。
EXCLUDED: dict[str, str] = {}

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail else ""))


#: 一段**明显超长**的执行器交代 —— 用来抓任何形式的截断
LONG_REPORT = (
    "导学单已生成并交付至产物库，以下是简要说明：\n\n---\n\n### 交付内容\n\n"
    "| 环节 | 设计思路 |\n|------|---------|\n"
    + "".join(f"| 环节{i} | 这是第 {i} 个环节的设计说明，写得很长，用来把总长度顶过任何可能的截断阈值 |\n"
              for i in range(1, 40))
    + "\n**结尾标记**：这一段在最后，截断的话它一定不在。"
)

#: 它**申报**要的那句原话（`ask_user` 的参数）—— 🔴 与 `LONG_REPORT` **故意不同**：
#: 【它要的信息】必须渲染**这一句**，不是那段独白（2026-11-04 日志定位）。
#: 两处用同一个值的话，"取错了源"这件事在断言上**看不出来**。
ASKED = "这份导学单主要用在哪？（课前预习 / 课中探究 / 课后巩固）"


#: 三态共用同一个产物 —— **只建一次**（每次都清库的话，先前那两条的 ID 就悬空了）
artifacts.clear()
AID = store_artifact("白桦-导学单", "课前预习用填空式导学单", "md", "正文" * 900)
artifacts.get(AID)["path"] = r"C:\runs\art_001_白桦-导学单.md"


def _render(outcome: ExecOutcome, **kw) -> str:
    """结果 → **回执对象** → 文本。**两跳都要走** —— 渲染只认对象，不认结果。"""
    return render_receipt(build_receipt(outcome, **kw))


def _outcome(status: str) -> ExecOutcome:
    return ExecOutcome(
        status=status,
        artifacts=[AID] if status == "completed" else [],
        report=LONG_REPORT,
        rounds=3,
        # 只有"在要信息"那一档才有申报 —— 其余档 `asked` 是空的（`absorb_outcome` 覆盖写）
        asked=ASKED if status == "waiting_for_input" else "",
    )


def main() -> None:
    completed = _outcome("completed")
    waiting = _outcome("waiting_for_input")
    failed = _outcome("incomplete")
    #: 平台故障那一档（2026-11-04 复审 N2/R8）—— 也是**一个可达出口**，
    #: 所以「声明的段都用到了」那条守卫要把它算进来（否则 `fault` 看着像死段）。
    faulted = ExecOutcome(status="incomplete", artifacts=[], report="", rounds=1,
                          fault="TimeoutError: 抖动", fault_kind="transient")

    print("① 三态各有自己的回执，且**每个字段都渲染了**")
    for label, out in (("completed", completed), ("waiting_for_input", waiting), ("incomplete", failed)):
        text = _render(out)
        check(f"[{label}] 有【交付】段", text.startswith("【交付】"), text[:60])
        # 🔴 回执第一行说的是**事实**（交了几样 / 它在要信息 / 什么都没给），
        #    **不是判决**（"✅ 已完成"那种是判定给的，判定退休了）—— 断的是"三态各不相同"。
        heads = {
            "completed": "它交付了",
            "waiting_for_input": "要信息",
            "incomplete": "既没交付产物、也没说要什么信息",
        }
        check(f"[{label}] 状态说成了人话", heads[label] in text)
        # ⚠️ 原来这里还分两路断"进度 / 缺失项只在没完成时才给" ——
        #    2026-11-05 判定退休，那两段（【已做到】/【还缺】）整块删了，这条随之作废。
        if label == "incomplete":
            check("[incomplete] `rounds` 出现在话里", str(out.rounds) in text)
        if label == "waiting_for_input":
            # 🔴 **它要的信息必须进回执**（2026-11-04 日志定位）：主 agent 的回复
            #    **写在 `finalize` 注入问题之前** —— 实测同一轮：回执 15:23:14.770
            #    → 它回复 15:23:19.193 → 平台注入问题 15:23:19.198。回执里没有这一句，
            #    它写"派了什么、还差什么"时就只能**编**（实测编出"比如年级、课时、
            #    学情等"，而执行器问的是**课型**）。透传保证用户不丢，但它那句话是假的。
            check("[waiting] 有【它要的信息】段", SECTION["asked"] in text, text[:200])
            check("[waiting] 段体是**申报的那句原话**（`ask_user` 的参数）", ASKED in text)
            check("[waiting] 它排在【执行器说】**前面**（问题比那段独白要紧）",
                  text.index(ASKED) < text.index(SECTION["report"]), text[:300])
        for aid in out.artifacts:
            check(f"[{label}] 产物 ID 渲染了", aid in text)
            check(f"[{label}] 产物**真实路径**渲染了", artifacts.get(aid)["path"] in text)

    # 🔴 `asked` 缺失时**不许静默降级**（fixture / 手写 state）——
    #    说清"这一条没留下问题原话"，再让【执行器说】把那段话摆出来（同 `finalize._one`）。
    _noq = ExecOutcome(status="waiting_for_input", artifacts=[], report="我先分析一下……",
                       rounds=1, asked="")
    _t = _render(_noq)
    check("`asked` 缺失时**显式说清**（不静默降级、也不假装有）",
          SECTION["asked"] in _t and "没留下问题原话" in _t, _t[:300])

    print("\n② 🔴 **一个字都不许截断** —— 这是这条规矩的由来")
    for label, out in (("completed", completed), ("waiting_for_input", waiting), ("incomplete", failed)):
        text = _render(out)
        # ⚠️ **按压平后的口径比**：段体渲染时会整块缩进 2 列（`handoff.BODY`），
        #    多行的原话就不再是连续子串了 —— 缩进是结构，不是内容，
        #    用 `_flat` 两边同一个口径（同 `unit_briefing` ⑦ 那条注释）。
        check(f"[{label}] 执行器交代**全文**都在", _flat(out.report) in _flat(text))
        check(f"[{label}] 结尾标记没丢", "**结尾标记**" in text)
        check(f"[{label}] 没有截断省略号", "…（" not in text and "…\n" not in text)

    print("\n③ 来源标注：**哪句是执行器说的、哪句是主 agent 说的**")
    # ⚠️ 2026-11-05 判定退休：原来这里还断"判定说"标了两处 —— 判定的段整块删了。
    text = _render(waiting, agent_answer="用户之前说过是四年级")
    check("【执行器说】标了是它说的", "**它说的**" in text)
    check("【你补的信息】标了是主 agent 说的", "**你说的**" in text)
    check("补的信息**全文**在（不截断）", "用户之前说过是四年级" in text)

    print("\n④ 条件出现：没有的段不许凭空冒出来")
    # ⚠️ "completed 但没有产物"**现在结构上不可能**（三态由平台按结构算：有产物才 completed）
    #    —— 所以这一档改成**真实的那一档**：没交也没申报 → incomplete。
    bare = ExecOutcome(status="incomplete", artifacts=[], report="成果在这里", rounds=3)
    t = _render(bare)
    check("没产物 → 没有【产出的产物】", "【产出的产物】" not in t)
    check("没交也没申报 → 交付行**说事实**（不是判决）",
          "既没交付产物、也没说要什么信息" in t)
    check("没申报要信息 → 没有【它要的信息】（条件段，不许凭空冒出来）",
          SECTION["asked"] not in t)
    check("没试答 → 没有【你补的信息】", "【你补的信息】" not in t)
    check("没给 note → 没有【下一步】", "【下一步】" not in t)
    check("成果在回复里时，正文**原样**在", "成果在这里" in t)

    print("\n⑤ 试答成功那条出口：补了什么要看得见（原来截到 60 字）")
    long_answer = "用户之前说过：" + "四年级下册第一课时" * 12
    t = _render(completed, agent_answer=long_answer)
    check("补的信息**全文**在", long_answer in t, f"长度 {len(long_answer)}")

    print("\n⑥ 记录层读的是**对象**，不是文本（用户 2026-11-03 定的硬规矩）")
    from evals.harness import _RECEIPT_LABEL, _receipt_sections
    # ⚠️ 只 import `HOPS`：`SECTION` **模块级已经 import 过了** —— 在函数里再 import 一次
    #    会把它变成**整个函数的局部名**，于是它前面那几处用法直接 `UnboundLocalError`（踩过）。
    from proto.handoff import HOPS
    from proto.handoff.receipt import build_rejection

    # ① 三态：对象里的段 = 渲染出来的段；**执行器的原话逐字相等**
    #    （老实现靠解析文本，这段根本切不出来 → 回执掉进 1200 字兜底截断）
    for label, out in (("completed", completed), ("waiting_for_input", waiting),
                       ("incomplete", failed)):
        r = build_receipt(out, agent_answer="补一句", note="（示例）下一步")
        text = _render(out, agent_answer="补一句", note="（示例）下一步")
        parts = _receipt_sections(r)
        check(f"[{label}] 对象切得出段", len(parts) >= 4,
              f"只切出 {len(parts)} 段：{[p[0][:12] for p in parts]}")
        got = [t for lab, t in parts if lab == _RECEIPT_LABEL["report"]]
        check(f"[{label}] 执行器的原话那一段 == `report` 逐字",
              bool(got) and got[0] == out.report.strip())
        check(f"[{label}] 渲染出的文本包含对象里每一段的内容",
              all(_flat(t) in _flat(text) for _, t in parts))

    # ①b 排版：**段名独占一行 · 段体 2 列**（和委托单**同一条规矩**，2026-11-03）
    #     ⚠️ 断言对象是**渲染结果**：换实现也照样管用。
    for label, out in (("completed", completed), ("waiting_for_input", waiting), ("incomplete", failed)):
        text = _render(out, agent_answer="补一句", note="（示例）下一步")
        stray = [ln for ln in text.splitlines()
                 if ln.startswith("【") and ln.strip() not in SECTION.values()]
        check(f"[{label}] 段名独占一行（后面不跟内容）", not stray, f"越界的行：{stray[:2]}")
        loose = [ln for ln in text.splitlines()
                 if ln.strip() and not ln.startswith(("【", BODY))]
        check(f"[{label}] 段体/续行都缩进 ≥2 列", not loose, f"越界的行：{loose[:2]}")

    # ② 段名 ⊆ `HOPS` 声明（**加了段忘了登记 → 这里红**）
    all_used: set[str] = set()
    for out in (completed, waiting, failed, faulted):
        all_used |= set(build_receipt(out, agent_answer="补一句", note="（示例）下一步")["sections"])
    declared = set(HOPS["receipt"])
    check("对象里的段都在 `HOPS['receipt']` 里声明过", all_used <= declared,
          f"没声明：{sorted(all_used - declared)}")
    check("声明的段**都用到了**（没有死段）", declared <= all_used,
          f"声明了没人用：{sorted(declared - all_used)}")

    # ③ 出口分类在**对象**上（`kind`），不靠解析文本
    check("三态 → `kind` 分得对",
          build_receipt(completed)["kind"] == "completed"
          and build_receipt(waiting)["kind"] == "waiting"
          and build_receipt(failed)["kind"] == "failed")
    # ⚠️ 原来这里还建了一个 `bare`（`completed` + 空产物）—— 它属于【成果在回复正文里】
    #    那一档；2026-11-05 判定退休后那个档位没了（`completed` 必然有产物），
    #    这行成了**定义了没人用**的死代码，删掉。

    # ③' 🔴 **回执里点到的产物 ID，必须是这次真产出的那些**（2026-11-05 实测踩到）：
    #     "要看就用 `read_artifact(...)` 读"那句原来**写死了 `art_001`** ——
    #     实测 `x-prep-lotus`：这次产出的是 `art_002`（导学单），提示却让人去读 `art_001`
    #     （那是上一份**教案**）。**指向"存在但内容不对"的出处，比指不到更坏**（同 E2 那条）。
    #     ⚠️ 这条**必须用一个不是 `art_001` 的产物**来断 —— 写死的那个正好撞上就测不出来了。
    #     （扫的是**平台自己渲染的模板**，不是 LLM 吐的东西 —— 确定性，能断言。）
    _a2 = store_artifact("荷花-导学单", "课前预习用", "md", "正文" * 50)
    _named = set(re.findall(r"art_\d+", _render(
        ExecOutcome(status="completed", artifacts=[_a2], report="交好了", rounds=1))))
    check("回执里点到的产物 ID 都是这次产出的（没有写死的）",
          _named == {_a2}, f"点到 {sorted(_named)}；这次产出 {_a2}")

    # ④ 派发被拒那几个口：对象 → 文本 → 记录层，三边一致
    # ⚠️ ~~`rejected_turns`~~ **2026-11-03 删**：轮次不再由主 agent 点名
    # （见 `briefing.turn_lines` 的分层说明），"点了不存在的轮次"结构上不可能了。
    rejected = {
        "rejected_skill": build_rejection("rejected_skill", "❌ 没有技能「x」", "a、b"),
        "rejected_artifact": build_rejection("rejected_artifact", "❌ 产物 art_9 不存在", "（无）"),
    }
    for kind, r in rejected.items():
        check(f"认得出来：{kind}", r["kind"] == kind)
        check(f"切得开：{kind}", len(_receipt_sections(r)) == 2, str(_receipt_sections(r))[:80])
        check(f"渲染得出：{kind}", render_receipt(r).startswith(SECTION["not_dispatched"]))

    # ④' 🔴 **审计层那两张手抄表必须覆盖得住**（2026-11-05 深度审计 §4.4）
    #     `_RECEIPT_KINDS` / `_RECEIPT_LABEL` 是**手抄** `DELIVERY` / `SECTION` 的 ——
    #     而"手抄"第一次加新出口（`fault`，2026-11-04）就漏了：`_run.md` 里那条回执
    #     **没有出口标记**、段标签**回落成裸 key `fault`**。没有任何守卫盯着覆盖性
    #     （不像 `HOPS` 有 ⊆ 校验）。这里补上 —— 名单**从真对象问出来**，不手抄。
    from evals.harness import _RECEIPT_KINDS, _RECEIPT_LABEL

    all_receipts = [build_receipt(o) for o in (completed, waiting, failed, faulted)]
    all_receipts += list(rejected.values())
    _missing_kind = sorted({r["kind"] for r in all_receipts} - set(_RECEIPT_KINDS))
    check("`_RECEIPT_KINDS` 覆盖得住每一个出口（漏一个 → 那条回执在 `_run.md` 里没标记）",
          not _missing_kind, f"漏了：{_missing_kind}")
    _used: set[str] = set()
    for _r in all_receipts:
        _used |= set(_r.get("sections") or {})
    _missing_label = sorted(_used - set(_RECEIPT_LABEL))
    check("`_RECEIPT_LABEL` 覆盖得住回执用到的每一段（漏一个 → 标签回落成裸 key）",
          not _missing_label, f"漏了：{_missing_label}")

    # ⑤ 走**真代码路径**：真 `delegate` 吐出来的消息上挂着对象，记录层读得到
    import proto.agent as A

    class _R:  # 假的 ToolRuntime —— 拒绝口只用到 state / tool_call_id
        state = {"messages": [], "tasks": []}
        tool_call_id = "call_abc12345"

    real = {
        "rejected_skill": asyncio.run(A.delegate.coroutine(
            skill_name="nope", task="x", runtime=_R())),
        "rejected_artifact": asyncio.run(A.delegate.coroutine(
            skill_name="quiz", task="x", artifacts=["art_9"], runtime=_R())),
    }
    for kind, cmd in real.items():
        msg = cmd.update["messages"][0]
        obj = (msg.additional_kwargs or {}).get("handoff")
        check(f"真 `delegate` 把对象挂在消息上：{kind}", bool(obj) and obj["kind"] == kind)
        check(f"对象切得开：{kind}", bool(obj) and len(_receipt_sections(obj)) == 2)


    # ⑥ 对象**不会漏给模型** —— 这是它敢挂在消息上的前提（实测 `_convert_message_to_dict`
    #    只取 content / role / tool_call_id）
    from langchain_openai.chat_models.base import _convert_message_to_dict

    d = _convert_message_to_dict(real["rejected_skill"].update["messages"][0])
    check("发给模型的 dict 里**没有** handoff 对象", "handoff" not in str(d), str(d))


    print("\n⑨ 平台故障：**独立出口**（不借 failed 的文案、不塞进【执行器说】）")
    # 🔴 2026-11-04 复审 N2/R8：故障时"推了 N 轮"是**假话**（一轮都没跑完），
    #    而且主 agent 该做的事完全不同（**原样**重派 vs 换说法重派）。
    r = build_receipt(faulted, note="这次是**平台故障**，原样再 delegate 一次即可")
    t = render_receipt(r)
    check("出口分类是 `fault`（不是 `failed`）", r["kind"] == "fault", repr(r["kind"]))
    check("交付行说的是**没跑成**，不是「推了 N 轮」",
          "平台故障" in t and "推了" not in t, repr(t.splitlines()[:3]))
    check("【平台故障】段在，且**说清是谁的锅**",
          "【平台故障】" in t and "基础设施抖动" in t and "不是任务本身的问题" in t)
    check("**给了明确的下一步**：原样重派（不是「换个说法」）",
          "原样再 `delegate` 一次" in t and "换个说法" not in t)
    check("故障原因原样带出来（复盘要看）", "TimeoutError: 抖动" in t)
    check("**没有**冒出一个【执行器说】（它一个字都没说）", "【执行器说】" not in t)

    # 🔴 **两类故障两种处置**（2026-11-06，外测 SX-07）：4xx（除 408/429）是上游对
    #    **这份内容**的确定性拒绝 —— "原样再派"对它就是**错的话**（重发会再被拒）。
    #    分类是**结构**（`fault_kind`，见 `handoff.FaultKind`），文案按它渲染；
    #    **不许从 `fault` 文本反解析**（判据一）。
    rejected = ExecOutcome(
        status="incomplete", artifacts=[], report="", rounds=1,
        fault="BadRequestError: Output data may contain inappropriate content.",
        fault_kind="rejected")
    rt = render_receipt(build_receipt(rejected, note=next_note("fault", fault_kind="rejected")))
    check("rejected：交付行说**上游拒绝**（不是'平台故障…重试过仍失败'）",
          "上游拒绝了这次请求" in rt and "重试过一次仍失败" not in rt,
          repr(rt.splitlines()[:3]))
    check("rejected：【下一步】说**换措辞**，且**没有**'原样再派'",
          "换措辞" in rt and "原样再 `delegate`" not in rt, repr(rt))
    # ⚠️ 断的是**那句假话本身**（"平台侧的基础设施抖动，重试过一次仍然失败"）——
    #    裸词"基础设施抖动"在否定句里是**正当的**（"不是基础设施抖动"），别误伤。
    check("rejected：【平台故障】段**没有**'平台侧的基础设施抖动…重试过'这句假话",
          "平台侧的基础设施抖动" not in rt and "重试过一次" not in rt, repr(rt))

    # 🔴 **第三档：超时**（2026-11-06，外审 F9 的墙钟）：它**一直在干活**，只是这一趟没干完 ——
    #    抖动那句（"重试过一次仍失败"）和拒绝那句（"原样重发会再被拒"）**对它都是假话**。
    #    ⚠️ 这一条是**实测逼出来的**：第一版只改了交付行/台账/判定，【平台故障】段
    #    还印着"重试过一次仍然失败"—— 渲染出来一看才发现（"四处引导句"是真四处）。
    timed_out = ExecOutcome(
        status="incomplete", artifacts=[], report="", rounds=2,
        fault="整趟超过墙钟上限（600s）", fault_kind="timeout", wall_s=612.0)
    tt = render_receipt(build_receipt(timed_out, note=next_note("fault", fault_kind="timeout")))
    check("timeout：交付行说**时间用完了**，并带上跑了多久",
          "时间用完了" in tt and "跑到 10 分钟" in tt, repr(tt.splitlines()[:3]))
    check("timeout：【下一步】说**原样再派可以**，且多一句「老超时就拆小」",
          "原样再 `delegate` 一次" in tt and "拆小" in tt, repr(tt))
    check("timeout：**没有**那两句假话（重试过 / 原样重发会再被拒）",
          "重试过一次" not in tt and "原样重发同一份内容会再被拒" not in tt, repr(tt))
    # ⚠️ `wall_s` 说不清（fixture / 手写 outcome）→ **不编数**（同 `Task.asked` 的规矩）
    _tt2 = render_receipt(build_receipt(
        ExecOutcome(status="incomplete", fault="x", fault_kind="timeout"),
        note=next_note("fault", fault_kind="timeout")))
    check("timeout：`wall_s` 缺失时**不编「跑到 0 分钟」**", "跑到" not in _tt2,
          repr(_tt2.splitlines()[:2]))
    check("transient（对照，⑨ 那条）：仍是'原样再派'", "原样再 `delegate` 一次" in t)

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
