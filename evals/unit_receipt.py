"""单元级：**回执**（执行器 → 主 agent）要形式化、要完整（不调 LLM）。

    uv run python -m evals.unit_receipt

**为什么要有它**（2026-11-02 用户）：

> "交付内容跟委托任务交付的东西一样，**得形式化**呀，不要那么随意懂吗。
>  而且**不要截断** —— 我很关心 subagent 给主 agent 交付了什么呀，你截断了我怎么看。"

它是 `unit_briefing.py` 的**镜像**：那边管交接书（平台 → 执行器），这边管回执
（执行器 → 主 agent）。两边的规矩是同一条 —— **每个字段要么渲染、要么显式排除**。

> 有效性自检：把 `render_receipt` 里任意一段删掉，本测试必须红；
> 把 `report` 换成 `report[:150]`，② 必须红（2026-11-02 手工验过）。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from proto.agent import render_receipt
from proto.artifacts import ARTIFACTS, store_artifact
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
ARTIFACTS.clear()
AID = store_artifact("白桦-导学单", "课前预习用填空式导学单", "md", "正文" * 900)
ARTIFACTS[AID]["path"] = r"C:\runs\art_001_白桦-导学单.md"


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
        text = render_receipt({}, out)
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
            check(f"[{label}] 产物**真实路径**渲染了", ARTIFACTS[aid]["path"] in text)

    print("\n② 🔴 **一个字都不许截断** —— 这是这条规矩的由来")
    for label, out in (("completed", completed), ("waiting_for_input", waiting), ("incomplete", failed)):
        text = render_receipt({}, out)
        check(f"[{label}] 执行器交代**全文**都在", out.report in text)
        check(f"[{label}] 结尾标记没丢", "**结尾标记**" in text)
        check(f"[{label}] 没有截断省略号", "…（" not in text and "…\n" not in text)

    print("\n③ 来源标注：**哪句是执行器说的、哪句是判定说的、哪句是主 agent 说的**")
    text = render_receipt({}, waiting, agent_answer="用户之前说过是四年级")
    check("【执行器说】标了是它说的", "**它说的**" in text)
    check("【已做到】【还缺】标了是判定说的", text.count("**判定说**") >= 2)
    check("【你补的信息】标了是主 agent 说的", "**你说的**" in text)
    check("补的信息**全文**在（不截断）", "用户之前说过是四年级" in text)

    print("\n④ 条件出现：没有的段不许凭空冒出来")
    bare = ExecOutcome(status="completed", artifacts=[], report="成果在这里", rounds=1,
                       progress="", missing=[])
    t = render_receipt({}, bare)
    check("没产物 → 没有【产出的产物】", "【产出的产物】" not in t)
    check("没产物 → 交付行说明了这件事", "没有产物" in t)
    check("没 progress/missing → 两段都不出", "【已做到】" not in t and "【还缺】" not in t)
    check("没试答 → 没有【你补的信息】", "【你补的信息】" not in t)
    check("没给 note → 没有【下一步】", "【下一步】" not in t)
    check("成果在回复里时，正文**原样**在", "成果在这里" in t)

    print("\n⑤ 试答成功那条出口：补了什么要看得见（原来截到 60 字）")
    long_answer = "用户之前说过：" + "四年级下册第一课时" * 12
    t = render_receipt({}, completed, agent_answer=long_answer)
    check("补的信息**全文**在", long_answer in t, f"长度 {len(long_answer)}")

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
