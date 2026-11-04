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
**没有**任何"把渲染出来的散文解析回段"的代码（那条路已经整个删掉）。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from proto.handoff import BODY, SECTION
from proto.handoff.briefing import _flat
from proto.handoff.receipt import build_receipt, render_receipt
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
        progress="已完成学习目标与预习任务",
        missing=["填写形式", "篇幅"],
    )


def main() -> None:
    completed = _outcome("completed")
    waiting = _outcome("waiting_for_input")
    failed = _outcome("incomplete")

    print("① 三态各有自己的回执，且**每个字段都渲染了**")
    for label, out in (("completed", completed), ("waiting_for_input", waiting), ("incomplete", failed)):
        text = _render(out)
        check(f"[{label}] 有【交付】段", text.startswith("【交付】"), text[:60])
        # status 是英文枚举，回执里该是**人话**；断的是"三态各不相同"
        heads = {
            "completed": "已完成",
            "waiting_for_input": "在请求信息",
            "incomplete": "失败",
        }
        check(f"[{label}] 状态说成了人话", heads[label] in text)
        # ⚠️ 进度 / 缺失项**只在"没完成"时才给**（完成了就不该再提"还缺"）——
        #    所以这里分两路断，不是漏测
        if label == "completed":
            check("[completed] 不该出现【还缺】（它已经完成了）", "【还缺】" not in text)
        else:
            check(f"[{label}] 【已做到】渲染了", out.progress in text)
            check(f"[{label}] 【还缺】逐条渲染了", all(x in text for x in out.missing))
        if label == "incomplete":
            check("[incomplete] `rounds` 出现在话里", str(out.rounds) in text)
        for aid in out.artifacts:
            check(f"[{label}] 产物 ID 渲染了", aid in text)
            check(f"[{label}] 产物**真实路径**渲染了", artifacts.get(aid)["path"] in text)

    print("\n② 🔴 **一个字都不许截断** —— 这是这条规矩的由来")
    for label, out in (("completed", completed), ("waiting_for_input", waiting), ("incomplete", failed)):
        text = _render(out)
        # ⚠️ **按压平后的口径比**：段体渲染时会整块缩进 2 列（`handoff.BODY`），
        #    多行的原话就不再是连续子串了 —— 缩进是结构，不是内容，
        #    用 `_flat` 两边同一个口径（同 `unit_briefing` ⑦ 那条注释）。
        check(f"[{label}] 执行器交代**全文**都在", _flat(out.report) in _flat(text))
        check(f"[{label}] 结尾标记没丢", "**结尾标记**" in text)
        check(f"[{label}] 没有截断省略号", "…（" not in text and "…\n" not in text)

    print("\n③ 来源标注：**哪句是执行器说的、哪句是判定说的、哪句是主 agent 说的**")
    text = _render(waiting, agent_answer="用户之前说过是四年级")
    check("【执行器说】标了是它说的", "**它说的**" in text)
    check("【已做到】【还缺】标了是判定说的", text.count("**判定说**") >= 2)
    check("【你补的信息】标了是主 agent 说的", "**你说的**" in text)
    check("补的信息**全文**在（不截断）", "用户之前说过是四年级" in text)

    print("\n④ 条件出现：没有的段不许凭空冒出来")
    bare = ExecOutcome(status="completed", artifacts=[], report="成果在这里", rounds=1,
                       progress="", missing=[])
    t = _render(bare)
    check("没产物 → 没有【产出的产物】", "【产出的产物】" not in t)
    check("没产物 → 交付行说明了这件事", "没有产物" in t)
    check("没 progress/missing → 两段都不出", "【已做到】" not in t and "【还缺】" not in t)
    check("没试答 → 没有【你补的信息】", "【你补的信息】" not in t)
    check("没给 note → 没有【下一步】", "【下一步】" not in t)
    check("成果在回复里时，正文**原样**在", "成果在这里" in t)

    print("\n⑤ 试答成功那条出口：补了什么要看得见（原来截到 60 字）")
    long_answer = "用户之前说过：" + "四年级下册第一课时" * 12
    t = _render(completed, agent_answer=long_answer)
    check("补的信息**全文**在", long_answer in t, f"长度 {len(long_answer)}")

    print("\n⑥ 记录层读的是**对象**，不是文本（用户 2026-11-03 定的硬规矩）")
    from evals.harness import _RECEIPT_LABEL, _receipt_sections
    from proto.handoff import HOPS, SECTION
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
    for out in (completed, waiting, failed):
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
    bare = ExecOutcome(status="completed", artifacts=[], report="成果在这里", rounds=1)
    check("正文交付 → `kind` 分得对", build_receipt(bare)["kind"] == "completed_in_reply")

    # ④ 派发被拒那几个口：对象 → 文本 → 记录层，三边一致
    # ⚠️ ~~`rejected_turns`~~ **2026-11-03 删**：轮次不再由主 agent 点名
    # （见 `briefing.turn_lines` 的分层说明），"点了不存在的轮次"结构上不可能了。
    rejected = {
        "rejected_skill": build_rejection("rejected_skill", "❌ 没有能力「x」", "a、b"),
        "rejected_artifact": build_rejection("rejected_artifact", "❌ 产物 art_9 不存在", "（无）"),
    }
    for kind, r in rejected.items():
        check(f"认得出来：{kind}", r["kind"] == kind)
        check(f"切得开：{kind}", len(_receipt_sections(r)) == 2, str(_receipt_sections(r))[:80])
        check(f"渲染得出：{kind}", render_receipt(r).startswith(SECTION["not_dispatched"]))

    # ⑤ 走**真代码路径**：真 `delegate` 吐出来的消息上挂着对象，记录层读得到
    import proto.agent as A

    class _R:  # 假的 ToolRuntime —— 拒绝口只用到 state / tool_call_id
        state = {"messages": [], "tasks": []}
        tool_call_id = "call_abc12345"

    real = {
        "rejected_skill": A.delegate.func(skill_name="nope", task="x", runtime=_R()),
        "rejected_artifact": A.delegate.func(skill_name="quiz", task="x", artifacts=["art_9"], runtime=_R()),
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


    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
