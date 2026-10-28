"""验证 completion_criteria（评审人 D1 的落点）。

**要证明的三件事**：

  A. 作者**声明了**完成条件 → 平台能按条目检查，而且**能指出具体缺哪条**
     （这是"完成状态由业务完成条件决定，产物库只是证据之一"）

  B. 产出**在向用户索要信息**时，也会被判为没满足条件
     （不能因为"有输出"就当完成）

  C. 作者**没声明**的 skill，照样能跑（走运行时兜底）—— **不写元数据不用改一个字**

    uv run python debug_criteria.py
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from proto import trace
from proto.agent import _check_criteria
from proto.config import describe
from proto.skills import SKILLS

# 导学单的完成条件：必须包含「学习目标」「预习任务」「当堂检测」
GL = SKILLS["guided-learning"]
CRITERIA = GL.completion_criteria

# ── 三段测试产出 ──

FULL = """三年级数学《分数的初步认识》第1课时 导学单

**学习目标**：理解平均分，认识几分之一，会读写简单分数。

**预习任务**：观察家里分蛋糕/切西瓜的场景，说说"平均分"是什么意思。

**当堂检测**：1. 把一个圆平均分成4份，每份是几分之几？ 2. 判断哪些图形是平均分。
"""

MISSING_ONE = """三年级数学《分数的初步认识》第1课时 导学单

**学习目标**：理解平均分，认识几分之一，会读写简单分数。

**预习任务**：观察家里分蛋糕/切西瓜的场景，说说"平均分"是什么意思。
"""

ASKING = "这份导学单是第几课时的？请告诉我课时安排，我再继续。"

CASES = [
    ("① 完整产出", FULL, True, None),
    ("② 缺「当堂检测」", MISSING_ONE, False, "当堂检测"),
    ("③ 在向用户索要信息", ASKING, False, None),
]


def run(label: str, text: str, expect_pass: bool, expect_missing: str | None) -> bool:
    trace.section(f"=== {label} ===")
    print(f"\n── {label} ──")
    print(f"   产出：{text[:50].strip()}…")

    r = _check_criteria(text, CRITERIA)
    print(f"   passed       = {r.passed}")
    print(f"   failed_items = {r.failed_items}")

    ok = r.passed == expect_pass
    if ok and expect_missing:
        ok = any(expect_missing in x for x in r.failed_items)
    if ok and not expect_pass:
        ok = bool(r.failed_items)  # 没通过时必须**指出**缺哪条，不能只给个 False

    if expect_pass:
        why = "✅ 判为完成"
    elif expect_missing:
        why = f"✅ 判为未完成，且**明确指出**缺「{expect_missing}」"
    else:
        why = "✅ 判为未完成，且列出了未满足的条目"
    print(f"   → {why if ok else '❌ 判定不符预期'}")
    return ok


# ── B 部分：判定分派（有/无 criteria × 有/无产物）──


def _mk(arts: list[str]) -> list[str]:
    """造几个真产物，供 `_judge_completion` 读正文。"""
    from proto.skills import store_artifact

    return [store_artifact(f"t{i}", c) for i, c in enumerate(arts)]


def judge_case(label: str, skill, arts: list[str], report: str, expect: bool) -> bool:
    from proto.agent import _judge_completion

    trace.section(f"=== {label} ===")
    print(f"\n── {label} ──")
    ok, note = _judge_completion(skill, arts, report)
    print(f"   有 criteria = {bool(skill.completion_criteria)}   产物数 = {len(arts)}")
    print(f"   判定 = {'完成' if ok else '未完成'}   附注 = {note[:60]!r}")
    good = ok == expect
    print(f"   → {'✅ 符合预期' if good else '❌ 不符预期'}")
    return good


if __name__ == "__main__":
    print(f"模型：{describe()}")
    print(f"日志：{trace.LOG_FILE}")
    print(f"\n检查用的完成条件（来自 {GL.name}）：")
    for i, c in enumerate(CRITERIA, 1):
        print(f"  {i}. {c}")

    # ── A：检查逻辑 ──
    results = [run(label, text, ep, em) for label, text, ep, em in CASES]
    print(f"\n{'─' * 70}")
    print(f"A 直接检查：{sum(results)}/{len(results)} 通过")

    # ── B：判定分派 ──
    from proto.skills import Skill

    WITH = GL  # 有 criteria
    WITHOUT = SKILLS["courseware-config"]  # 无 criteria

    print(f"\n{'─' * 70}")
    print("B 判定分派（有/无 criteria × 有/无产物）")

    b_results = [
        # 有 criteria：落库产物**也要**检查（评审人 C2：生成了文件但缺章节 ≠ 完成）
        judge_case("有criteria · 产物完整", WITH, _mk([FULL]), "已写入", True),
        judge_case("有criteria · 产物缺章节", WITH, _mk([MISSING_ONE]), "已写入", False),
        judge_case("有criteria · 无产物·回复已交付", WITH, [], FULL, True),
        judge_case("有criteria · 无产物·回复在问", WITH, [], ASKING, False),
        # 无 criteria：走既有事实 / 运行时兜底 —— **不写元数据的 skill 一个字不用改**
        judge_case("无criteria · 有产物（既有事实）", WITHOUT, _mk(["配置块内容"]), "已写入", True),
        judge_case("无criteria · 无产物·回复已交付", WITHOUT, [], "```config\n{}\n```", True),
    ]

    print(f"\n{'─' * 70}")
    print(f"A 直接检查：{sum(results)}/{len(results)}")
    print(f"B 判定分派：{sum(b_results)}/{len(b_results)}")
    print(f"合计：{sum(results) + sum(b_results)}/{len(results) + len(b_results)}")

    no_criteria = [s.name for s in SKILLS.values() if not s.completion_criteria]
    print(f"\n未声明完成条件的 skill：{no_criteria or '（无）'}")
    print("→ 这些 skill **一个字都不用改**，照样跑")

    trace.close()
    print(f"\n完整日志：{trace.LOG_FILE}")
