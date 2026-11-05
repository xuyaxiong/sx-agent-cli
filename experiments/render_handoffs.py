"""**生成 `docs/handoff-samples.md`** —— 每个交接单一张**真渲染出来的**样例。

    uv run python -m experiments.render_handoffs

🔴 **为什么要生成、不手写**（用户 2026-11-05）：

> *"每个例子搞个样例，样例最好是真的代码渲染出来的 —— 现在**数据和渲染分离**
> 这个应该不难做到。"*

手写的样例**必然陈旧** —— 而且它是"改一句文案、下游静默失效"的另一种形态
（这次下游是**文档**：2026-11-05 术语统一时就撞到 6 处文档里引用的旧文案）。
数据和渲染已经分层了，样例就能**生成**：渲染器改一次，重跑一次，样例跟着变。

⚠️ **它不调 LLM** —— 全是构造出来的 fixture。
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT = Path(__file__).resolve().parent.parent / "docs" / "handoff-samples.md"

Q = "这节课你希望侧重哪种上法？（探究发现型 / 讲解示范型 / 练习巩固型 / 混合型，也可以自由说）"
TASK = "生成人教版小学语文《荷花》一课的教案。请按标准教案结构输出。"


def _fixtures() -> dict[str, object]:
    """造一套 fixture —— **同一个场景贯穿全部样例**（荷花那节课）。"""
    from langchain_core.messages import AIMessage, HumanMessage

    from proto import artifacts
    from proto.artifacts import store_artifact
    from proto.executor import ExecOutcome

    artifacts.clear()
    aid = store_artifact(
        "《荷花》教案",
        "人教版小学语文三年级下册《荷花》一课完整教案，课型为探究发现型，适合基础一般的学生。",
        "md", "正文" * 900)
    artifacts.get(aid)["path"] = r"C:\runs\art_001_荷花-教案.md"

    task = {
        "id": "t86e687aa", "skill": "lesson-prep", "task": TASK,
        "stage": "waiting_for_input", "artifacts": [], "refs": [aid],
        "asked": Q, "report": "好的，第一个问题已发出，等你的答复。", "run_by": "delegate",
        "user_turns": [
            {"n": 1, "text": "我现在想生成人教版小学语文荷花这一课的教案和导学单，注意导学单内容得参考教案内容", "after": ""},
            {"n": 2, "text": "探究发现型", "after": f"【lesson-prep】问：{Q}"},
        ],
        "qa": [{"by": "用户", "q": Q, "a": "探究发现型"}],
    }
    done = {**task, "stage": "completed", "artifacts": [aid], "asked": ""}
    failed = {**task, "stage": "failed", "asked": "",
              "report": "（它说了一些话，但既没交付也没申报）", "run_by": "feed"}
    return {
        "task": task, "done": done, "failed": failed, "aid": aid,
        "wait": ExecOutcome(status="waiting_for_input", artifacts=[], rounds=1,
                            report=task["report"], asked=Q),
        "completed": ExecOutcome(status="completed", artifacts=[aid], rounds=1,
                                 report="教案已生成并交付至产物库（`art_001`），包含完整的教学目标、"
                                        "教学重难点、教学过程、板书设计、课后作业及教学反思。"),
        "incomplete": ExecOutcome(status="incomplete", artifacts=[], rounds=3,
                                  report="我尽力了，但没做完。"),
        "faulted": ExecOutcome(status="incomplete", artifacts=[], rounds=1, report="",
                               fault="TimeoutError: 网关抖动"),
        "msgs": [HumanMessage("我现在想生成人教版小学语文荷花这一课的教案和导学单"),
                 AIMessage("好的！教案先行，导学单随后参考教案来生成。先来教案。")],
    }


def _samples(f: dict) -> list[tuple[str, str, str, str]]:
    """→ [(编号, 标题, 说明, 渲染出来的正文)]。"""
    from proto.graph import render_relay
    from proto.handoff import render_hop
    from proto.handoff.ask import build_prompt
    from proto.handoff.briefing import render_briefing
    from proto.handoff.ledger import render_ledger
    from proto.handoff.nudge import nudge_text
    from proto.handoff.receipt import build_receipt, build_rejection, render_receipt

    task, done, failed = f["task"], f["done"], f["failed"]
    return [
        ("H4 ＋ H5", "任务文本（平台 → 执行器）★ **执行器打开就看的那一页**",
         "两个头：【技能指令】＋【已知信息】。⚠️ 交接书**整块缩在**【已知信息】下面 —— "
         "在这之前两者**同在 0 列**，读的人分不出哪几段属于已知信息。",
         render_hop("task_text", {
             "skill_instructions": "你是一位经验丰富的教师，请为指定课题生成一份完整的教案。\n"
                                   "（技能指令全文 2532 字，此处略）",
             "known": render_briefing(task)})),

        ("H4", "交接书（平台 → 执行器）",
         "任务文本里【已知信息】那一段**单独看**就是这个。三条路径（首跑 / 试答后重跑 / "
         "答复投递）**共用这一份**。",
         render_briefing(task)),

        ("H10", "回执 · completed（执行器 → 主 agent）",
         "★ 交接书的**镜像**。段是**有条件**的：有产物才出【产出的产物】。",
         render_receipt(build_receipt(f["completed"], note=(
             "**你判**：这次交付够不够？（不够就换个说法重派，或向用户说明）\n"
             "要核对内容就用 `read_artifact('<产物 ID>')` 读全文 —— 正文不在你的上下文里。")))),

        ("H10", "回执 · waiting（**决策单**：结果回给执行器，还是问用户）",
         "`finalize` 会把【它要的信息】原样贴给用户；主 agent **这一轮不发言**"
         "（`agent._StopWhenPending`）—— 所以它不会复述。",
         render_receipt(build_receipt(f["wait"], note=(
             "平台已登记为待答，**并会负责把这个问题原样转达给用户**，"
             "用户的答复也会由平台投回给它 —— 你**不用**重复问，更不要自己替用户定一个答案。")))),

        ("H10", "回执 · failed",
         "推满轮次、既没交产物也没申报。两种可能（真干不了 / 把答案写在回复里了），"
         "**由主 agent 看回执定**。",
         render_receipt(build_receipt(f["incomplete"], note=(
             "它推满了轮次**既没交产物、也没说要什么信息**。两种可能，你看回执定。")))),

        ("H10", "回执 · fault（**平台故障**）",
         "⚠️ **独立出口**：这一轮**根本没跑成**，与「执行器干不了」是两回事 —— "
         "所以不借 failed 的文案（「推了 N 轮」对故障是假话），也不塞进【执行器说】。",
         render_receipt(build_receipt(f["faulted"], note=(
             "这次是**平台故障**，**不是任务本身的问题** —— 原样再 `delegate` 一次即可。")))),

        ("H10", "回执 · rejected_skill（**派发被拒**）",
         "⚠️ 它**不建 Task** —— 主 agent 编了一个不存在的技能名，当场拒绝并列出真实清单。"
         "（另一条 `rejected_artifact` 同形，只是列的段换成【本会话现有产物】。）",
         render_receipt(build_rejection("rejected_skill", "❌ 没有技能「lesson-plan」",
                                        "lesson-prep、guided-learning"))),

        ("H12", "台账（平台 → 主 agent）",
         "**唯一「故意短」的一跳**：每轮开头重复注入的索引，所以允许截断（但要带标记＋"
         "说清全文在哪）。⚠️ 它原来续行缩进 **6 格**、段名后面直接跟内容 —— "
         "和别的单子不是一套。",
         render_ledger([task, done])),

        ("H12", "台账 · 有失败任务时",
         "失败那档的明细行是「执行器说」（它最后说的那段）；待答那档是「它要的信息」"
         "（`ask_user` 的参数原话）—— **两档打的字段不一样**。",
         render_ledger([failed, done])),

        ("H11", "试答（平台 → 主 agent）★ **决策单**",
         "要决定的是「**我知不知道它要的这个信息**」。⚠️ 它原来是**平铺的 `字段：值`**："
         "没有段名、`HumanMessage:` 这种**框架类名直接漏给模型**、`请判断` 夹在最后。",
         build_prompt("lesson-prep", TASK, f["wait"], f["msgs"])),

        ("H7", "推回去（平台 → 执行器）",
         "react 循环内：这一轮**既没交东西、也没申报**时喂回去。只说平台**自己看得见的两件事实**"
         "＋**两条出路**（判定退休后没有「已做到／还缺」了，不编）。",
         nudge_text()),

        ("H13", "待答透传（平台 → 用户）",
         "⚠️ 它原来每条写成 `【{skill}】问：…` —— **拿段名的括号包技能名** ✗。"
         "现在段名是【待答】，技能名用 `「」`。",
         render_relay([task])),
    ]


#: 23 个交接点里**有渲染产物**的那些 —— `(H, 交接, 从→到, 渲染器)`。
#: ⚠️ 没渲染产物的（H1 原话 / H3 工具参数 / H9 枚举 / H17 快照 …）**不在这张表里**，
#: 它们要么是原始文本，要么是结构字段 —— **没有「单子」可看**。
_HOPS_TABLE = """
| H | 交接 | 从 → 到 | 渲染器 | 形态 |
|---|---|---|---|---|
| **H2** | 系统提示 + 技能清单 | 平台 → 主 agent | `registry.catalog()` ＋ `_MAIN_PROMPT` | 1 |
| **H4** | **交接书** | 平台 → 执行器 | `render_hop("briefing")` | 1 |
| **H4＋H5** | **任务文本**（技能指令 ＋ 交接书） | 平台 → 执行器 | `render_hop("task_text")` | 1 |
| H6 | 产出落库回执 | 执行器 → 平台 | `write_artifact` 的返回 | 1 |
| **H7** | **推回去** | 平台 → 执行器 | `render_hop("nudge")` | 1 |
| **H10** | **回执** | 执行器 → 主 agent | `render_hop("receipt")` / `receipt_rejected` | **6** |
| **H11** | **试答** | 平台 → 主 agent | `render_hop("ask")` | 1 |
| **H12** | **台账** | 平台 → 主 agent | `render_hop("ledger")` | 2 |
| **H13** | **待答透传** | 平台 → 用户 | `render_hop("relay")` | 1 |
| H14 | 答复投递 | 平台 → 执行器 | **＝ H4**（同一份交接书） | — |
| H15 | 读产物 | 产物库 → 主 agent | `read_artifact` 的返回 | 1 |
| H16 | 读技能 / 技能文件 | 技能包 → 调用方 | `read_skill` / `read_skill_file` 的返回 | 2 |
| H21 | 评分材料 | judge → 评分模型 | `judge._reports` | 1 |
"""

_FORM = """
## 形式（四条，`proto/handoff/__init__.py` 里定的）

    段名 0 列（独占一行）· 来源 2 列 · 段体 2 列 · 每层 +2

**渲染只有一条路**：`render_hop(hop, sections)` —— 段名从 `SECTION` 取、
来源从 `SOURCE_NOTE` 取、**顺序从 `HOPS[hop]` 取**、缩进用 `BODY`。
没有第二种写法，也就没有"两套"可失配。

守卫：`evals/unit_handoff_form.py`（41 条）—— 其中第 ⑤ 条**扫源码**：
段名行不许手拼，只许从 `SECTION` 取。

## 🔴 什么算「交接单」——**只有下面第一类**

> 用户 2026-11-05：*"我们现在既然澄清了哪些是所谓的**交接清单**，
> 你就应该在文档里纠正以前的误区啊，不然下次又出现理解上的偏差。"*

| 类 | 是什么 | 有单子吗 | 例 |
|---|---|---|---|
| **① 交接单** | 平台**按形式排**出来的一段文本 | ✅ **走 `render_hop`** | 任务文本 · 交接书 · 回执 · 台账 · 试答 · 推回去 · 透传 |
| ② **原始文本** | 谁写的就原样搬，平台不排 | ❌ | 用户原话 · `ask_user` 的参数 · `delegate(task=…)` |
| ③ **结构字段** | 对象搭同一趟车，**不渲染** | ❌ | 三态 · `Task` 快照 · 回执对象 |
| ④ **工具返回** | 工具的返回值 | ❌ **且不许用 `【…】`**（那会冒充段名） | `read_artifact` · `read_skill` · `list_artifacts` |
| ⑤ **记录 / 评测层** | 人复盘看的、评分模型看的 | ❌ | trace 日志 · `_run.md` · 评分材料（H21） |

⚠️ **三个"跳数"数的是不同范围，别混**：

| 数 | 范围 | 在哪 |
|---|---|---|
| **7** | **有单子的**（本文件） | 这里 |
| **13** | **产品运行期**的交接 | `architecture-diagram.md` §3.1 |
| **23** | **全部**（＋执行器内部循环 / 快照重放 / 记录层 / 评测层） | `handoff-audit.md` |

**⇒ 别拿单子的形式规矩去要求工具返回**，也别以为工具返回该有个段名。
"""


def main() -> None:
    f = _fixtures()
    parts = [
        "# 交接单样例（**代码渲染出来的**）\n",
        "> 🔴 **这份文件是生成的，不要手改** —— `uv run python -m experiments.render_handoffs`\n"
        "> 手写的样例必然陈旧，而那是「改一句文案、下游静默失效」的另一种形态（这次下游是文档）。\n"
        "> 数据和渲染已经分层了，样例就能**生成**：渲染器改一次，重跑一次。\n",
        _FORM,
        "\n## 一、一共有多少个交接点\n",
        "**23 个**（H1–H23，编号沿用 `handoff-audit.md`）。其中**有渲染产物**的：\n",
        _HOPS_TABLE,
        "\n> ⚠️ 没列进来的（H1 用户原话 / H3 派发参数 / H8 `ask_user` 参数 / H9 三态 / "
        "H17 Task 快照 / H18 `results.json` / H19–H20 `_run.md` / H22 日志 / H23 断言 detail）\n"
        "> —— 它们要么是**原始文本**、要么是**结构字段**，**没有「单子」可看**。\n",
        "\n## 二、每一张单子的样例\n",
        "> 全部用**同一个场景**（荷花那节课）渲染，方便横向比。\n",
        "> ⚠️ **下面 12 张是走 `render_hop` 的「单子」**。表里还有几处**没采样** ——\n"
        "> H2（技能清单：系统提示里的一段列表）、H6 / H15 / H16（**工具返回**：\n"
        "> `write_artifact` / `read_artifact` / `read_skill`）、H21（评分材料，评测层）。\n"
        "> **它们不走 `render_hop`，也不该走** —— 段名/来源/缩进那套规矩是给**交接单**定的，\n"
        "> 工具返回不是单子（它们**不许用 `【…】`**，那会冒充段名 —— 见 `unit_handoff_form` ⑤）。\n",
    ]
    for i, (h, title, why, text) in enumerate(_samples(f), 1):
        parts.append(f"\n### {i}. {title}\n\n`{h}`\n\n{why}\n\n"
                     f"```\n{text}\n```\n")
    OUT.write_text("\n".join(parts), encoding="utf-8")
    print(f"已生成 {OUT}（{len(_samples(f))} 张样例）")


if __name__ == "__main__":
    main()
