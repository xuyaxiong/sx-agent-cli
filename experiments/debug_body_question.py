"""诊断：执行器**用正文提问**（不调 finish、不 interrupt）时，系统能不能接住？

这模拟的是 sx-agent 现在的做法：提问写进回复正文。
正文没有结构化信号，系统无法区分「我在问」和「我干完了」——
本脚本验证兜底方案：**补一次 LLM 判断，把正文翻译成 needs_input 协议**。

翻译完成后，后续路由与 finish 路径完全一样（都写 awaiting）。
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from proto import trace
from proto.config import describe
from proto.skills import ARTIFACTS, Deliverable, Skill, run_skill

# 故意做成「正文提问」风格：明确要求不许调用 finish，直接在正文里问
BODY_ASK_SKILL = Skill(
    name="body-ask-demo",
    label="随堂小测",
    required=["年级", "学科"],
    runtime_required=["难度"],
    deliverables=(Deliverable("随堂小测", "artifact"),),
    system_prompt=(
        "你是随堂小测执行器。\n"
        "参数齐了就正常出题。\n"
        "**重要**：如果缺少「难度」，不要调用任何登记工具（不要调 finish），"
        "直接在回复正文里用一句话向用户提问，问清楚难度要「基础 / 提升 / 挑战」哪一档。"
    ),
)


# 「只说不做」风格：正文声称完成，但不写任何产物 —— 必须判为 failed
EMPTY_TALK_SKILL = Skill(
    name="empty-talk-demo",
    label="随堂小测",
    required=["年级", "学科"],
    deliverables=(Deliverable("随堂小测", "artifact"),),
    system_prompt=(
        "你是随堂小测执行器。\n"
        "**不要调用任何工具**，直接在回复正文里告诉用户你已经把题出好了。"
    ),
)


def case(label: str, params: dict[str, str], *, expect: str, skill: Skill | None = None) -> None:
    """expect: 'needs_input' / 'done' / 'failed'。"""
    before = set(ARTIFACTS)
    trace.section(f"=== {label} ===")
    print(f"\n── {label} ──")
    print(f"   入参：{params}")

    r = run_skill(skill or BODY_ASK_SKILL, params)

    new = [k for k in ARTIFACTS if k not in before]
    print(f"   结果：status={r.status}")
    print(f"         missing_fields={r.missing_fields}")
    print(f"         done_so_far={r.done_so_far[:80]!r}")
    print(f"         新产物={new or '（无）'}")

    if expect == "needs_input":
        ok = r.status == "needs_input" and bool(r.missing_fields)
        print(
            "   → ✅ 正文提问被接住，翻译成了 needs_input 协议"
            if ok
            else "   → ❌ 没接住（正文提问被当成了「干完了」）"
        )
    elif expect == "done":
        ok = r.status == "done" and bool(new)
        print(
            "   → ✅ 参数齐全，正常交付（没有误判成 needs_input）"
            if ok
            else "   → ❌ 误判成 needs_input 或没交付"
        )
    else:
        ok = r.status == "failed"
        print(
            "   → ✅ 正确判为 failed（正文说完成了，但产物库是空的，不放行）"
            if ok
            else f"   → ❌ 竟然放行了（status={r.status}）—— 用户会看到「已交付」却什么也没拿到"
        )


if __name__ == "__main__":
    print(f"模型：{describe()}")
    print(f"日志：{trace.LOG_FILE}")

    # 缺「难度」→ 执行器应该在正文里问 → 兜底应翻译成 needs_input
    case("缺参数：执行器走正文提问", {"年级": "三年级", "学科": "数学"}, expect="needs_input")

    # 参数齐 → 应该正常出题（此时不该误判成 needs_input）
    case("参数齐全：不该误判", {"年级": "三年级", "学科": "数学", "难度": "基础"}, expect="done")

    # 只说不做 → 正文声称完成但没产物，必须判 failed，不能放行
    case(
        "只说不做：正文说完成了但没产物",
        {"年级": "三年级", "学科": "数学", "难度": "基础"},
        expect="failed",
        skill=EMPTY_TALK_SKILL,
    )

    trace.close()
    print(f"\n完整日志（含发给 LLM 的全文与它的原始回复）：\n  {trace.LOG_FILE}")
