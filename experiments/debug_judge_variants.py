"""对比四种「完成判定」提示词，看谁判得准。

**背景**：`completion_criteria` 本质也是 LLM 判断（软机制），
那它相对"把 instructions 带进提示词"有没有增量价值？

**四个变体**（同一套 schema，**只变提示词**，保证可比）：

  A · 只给产出             —— 现状（无 criteria 时）
  B · 产出 + instructions  —— 我建议的（要求本来就在 instructions 里）
  C · 产出 + criteria      —— 现状（有 criteria 时）
  E · 产出 + instructions + **反省式提问** —— 用户建议的

**六个用例**，每个都有明确的 ground truth：

    uv run python debug_judge_variants.py
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from typing import Literal

from pydantic import BaseModel, Field

from proto import trace
from proto.config import describe
from proto.llm import safe_structured

# ──────────────────────────── 输出 schema（四个变体共用） ────────────────────────────


class J(BaseModel):
    reflection: str = Field(default="", description="先反省：产物是否 OK / 执行是否真的完成")
    completed: bool = Field(description="这个任务完成了吗")
    asking_user: bool = Field(default=False, description="如果没完成：是在向用户索要信息吗")


# ──────────────────────────── 六个用例 ────────────────────────────

GL_INSTR = "产出导学单要点：学习目标、预习任务、当堂检测。精简，150 字以内。"
GL_CRIT = ("必须包含「学习目标」", "必须包含「预习任务」", "必须包含「当堂检测」")

FULL = """三年级数学《分数的初步认识》第1课时 导学单

**学习目标**：理解平均分，认识几分之一，会读写简单分数。
**预习任务**：观察家里分蛋糕的场景，说说"平均分"是什么意思。
**当堂检测**：1. 圆平均分成4份，每份是几分之几？ 2. 判断哪些图形是平均分。"""

MISSING_ONE = """三年级数学《分数的初步认识》第1课时 导学单

**学习目标**：理解平均分，认识几分之一，会读写简单分数。
**预习任务**：观察家里分蛋糕的场景，说说"平均分"是什么意思。"""

ASKING = "这份导学单是第几课时的？请告诉我课时安排，我再继续。"

# 有产物，但内容是残次品（占位没填完）
RUBBISH = """三年级数学《分数的初步认识》第1课时 导学单

**学习目标**：待补充
**预习任务**：待补充
**当堂检测**：待补充

（说明：由于不知道学校本周的教研主题，这份导学单无法完成。）"""

# 纯分析任务：本来就不产出文件
ANALYZE_INSTR = "分析这份教案的优缺点，**直接在回复里给出结论**，不要产出文件。"
ANALYZE_OK = """这份教案的优点：目标清晰、环节完整、有分层练习。
缺点：缺少学情分析；板书设计过于简略。
建议：补充前测环节，把板书拆成主板书+副板书。"""

NOTHING = "好的，我了解了。"

CASES = [
    dict(name="完整导学单", instr=GL_INSTR, crit=GL_CRIT, n_art=0, report=FULL,
         done=True, asking=False),
    dict(name="缺「当堂检测」", instr=GL_INSTR, crit=GL_CRIT, n_art=0, report=MISSING_ONE,
         done=False, asking=False),
    dict(name="在问用户", instr=GL_INSTR, crit=GL_CRIT, n_art=0, report=ASKING,
         done=False, asking=True),
    dict(name="有产物但是残次品", instr=GL_INSTR, crit=GL_CRIT, n_art=1, report=RUBBISH,
         done=False, asking=False),
    dict(name="纯分析·无产物但完成", instr=ANALYZE_INSTR, crit=(), n_art=0, report=ANALYZE_OK,
         done=True, asking=False),
    dict(name="什么都没干", instr=GL_INSTR, crit=GL_CRIT, n_art=0, report=NOTHING,
         done=False, asking=False),
]


# ──────────────────────────── 四个提示词变体 ────────────────────────────


def prompt_A(c) -> str:
    return (
        f"一个执行者完成了它的工作，产出如下：\n---\n{c['report']}\n---\n\n"
        "请判断：\n"
        "1. 这个任务完成了吗？（按任务要求判断，不要只看有没有产出文件）\n"
        "2. 如果没完成：它是在向用户索要信息吗？"
    )


def prompt_B(c) -> str:
    return (
        f"【任务要求】\n{c['instr']}\n\n"
        f"【执行者的产出】\n---\n{c['report']}\n---\n\n"
        "请判断：\n"
        "1. 这个任务完成了吗？（**对照上面的任务要求**）\n"
        "2. 如果没完成：它是在向用户索要信息吗？"
    )


def prompt_C(c) -> str:
    items = "\n".join(f"- {x}" for x in c["crit"])
    return (
        f"一个执行者完成了它的工作，产出如下：\n---\n{c['report']}\n---\n\n"
        f"【这个任务声明的完成条件（逐条核对，全满足才算完成）】\n{items}\n\n"
        "请判断：\n"
        "1. 这个任务完成了吗？（逐条核对上面的完成条件，**全部满足**才算完成）\n"
        "2. 如果没完成：它是在向用户索要信息吗？"
    )


def prompt_E(c) -> str:
    """**反省式**（用户建议）：先反省产物/执行，再判断完成。"""
    art_line = (
        f"执行者**产出了 {c['n_art']} 个文件**。" if c["n_art"]
        else "执行者**没有产出任何文件**。"
    )
    return (
        f"【任务要求】\n{c['instr']}\n\n"
        f"【执行者的产出】\n---\n{c['report']}\n---\n\n"
        f"{art_line}\n\n"
        "请**先反省，再判断**：\n"
        "1. **如果有产物**：这些产物是否**全部 OK**？逐一看——内容完整吗？"
        "符合上面的任务要求吗？有没有「待补充」「占位」「半成品」这类痕迹？\n"
        "2. **如果没有产物**：这个任务**本来就不需要产出文件**吗？"
        "如果是，**执行是否真的完成了**——而不是只说了句「我了解了」就收尾？\n"
        "3. 综合上面的反省：**这个任务完成了吗**？\n"
        "4. 如果没完成：它是在向用户索要信息吗？"
    )


def prompt_F(c) -> str:
    """**用户拟的模板**（三态 + 明确规则）—— 现在生产用的就是这个。"""
    return (
        f"【任务要求】\n{c['instr']}\n\n"
        f"【实际执行结果】\n---\n{c['report']}\n---\n\n"
        f"根据任务要求和实际执行结果，判断任务状态：\n"
        "- completed：任务已完成，且满足必要的交付条件。\n"
        "- waiting_for_input：任务尚未完成，且明确需要用户提供必要信息才能继续。\n"
        "- incomplete：任务尚未完成，但不属于等待用户提供信息的情况。\n"
        "\n"
        "判定规则：\n"
        "1. 需要文件产物的任务，必须检查产物是否存在、完整且符合要求；"
        "占位内容、待补充项或半成品不能视为完成。\n"
        "2. 不需要文件产物的任务，必须确认实际工作已经完成，"
        "不能仅凭执行者声称完成或输出确认性文字判定。\n"
        "3. 只有明确向用户索要完成任务所必需的信息，才能判定为 waiting_for_input。"
        "解释失败原因、表达困惑或描述缺少的信息，不等于已经向用户提问。\n"
    )


VARIANTS = {"A 只给产出": prompt_A, "B +instructions": prompt_B,
            "C +criteria": prompt_C, "E +instructions+反省": prompt_E,
            "F 用户模板(三态)": prompt_F}


class J3(BaseModel):
    """三态 schema —— 用户模板用的（F 变体）。"""

    status: Literal["completed", "waiting_for_input", "incomplete"]
    reason: str = ""


def run_one(vname: str, fn, c: dict) -> tuple[bool, bool]:
    prompt = fn(c)
    if vname.startswith("F"):
        r = safe_structured(J3, prompt, J3(status="incomplete"), tag=f"{vname} · {c['name']}")
        done = r.status == "completed"
        asking = r.status == "waiting_for_input"
    else:
        r = safe_structured(
            J, prompt, J(completed=False, asking_user=False, reflection="调用失败"),
            tag=f"{vname} · {c['name']}",
        )
        done, asking = r.completed, r.asking_user
    ok_done = done == c["done"]
    ok_ask = (asking == c["asking"]) if not c["done"] else True
    return ok_done, ok_ask


if __name__ == "__main__":
    print(f"模型：{describe()}")
    print(f"日志：{trace.LOG_FILE}\n")

    scores: dict[str, list[tuple[str, bool, bool]]] = {v: [] for v in VARIANTS}
    for vname, fn in VARIANTS.items():
        for c in CASES:
            d, a = run_one(vname, fn, c)
            scores[vname].append((c["name"], d, a))

    print(f"\n{'═' * 78}")
    print("结果（✓ = 判定与 ground truth 一致）")
    print(f"{'═' * 78}")
    header = f"{'用例':<22}" + "".join(f"{v:<20}" for v in VARIANTS)
    print(header)
    print("─" * 78)
    for i, c in enumerate(CASES):
        row = f"{c['name']:<22}"
        for v in VARIANTS:
            _, d, a = scores[v][i]
            mark = "✓" if (d and a) else ("△完成判对" if d else "✗")
            row += f"{mark:<20}"
        print(row)

    print("─" * 78)
    row = f"{'合计（全对）':<22}"
    for v in VARIANTS:
        n = sum(1 for _, d, a in scores[v] if d and a)
        row += f"{n}/{len(CASES):<18}"
    print(row)

    trace.close()
    print(f"\n完整日志：{trace.LOG_FILE}")
