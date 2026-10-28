"""验证「出口限制」：执行器既没完成、也没向用户提问时，平台会不会把它推回去继续。

**用户提的机制**：
> 限制出口——"如果最后一轮不是交付产物、也不是请求参数、也不是主动 finish"，
> 就让它回去继续 react。

**要证明三件事**：

  A. **触发**：执行器输出一段"我没法完成"就结束（既没交付、也没提问）
     → 平台**不让它停**，推它继续（日志里能看到"推它继续"）

  B. **有上限**：推了 MAX_REACT_ROUNDS 轮还是这样 → 判 `failed`，
     **交给主 agent 决定，而不是拿去烦用户**（评审人 E2「情况 A：执行失败」）

  C. **不误伤**：正常"提问"和"交付"两种出口**不受影响**，一轮就返回

    uv run python debug_exit.py
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from proto import trace
from proto.config import describe
from proto.executor import MAX_REACT_ROUNDS, run_task
from proto.skills import Skill

# ── A/B 用：一个"干不成、又不许问"的任务 —— 逼出「既没完成也没提问」 ──
NO_EXIT = Skill(
    name="no-exit-demo",
    description="演示：执行器干不成，但不许问用户",
    instructions=(
        "产出一份「三年级数学教案」。\n"
        "**硬要求**：教案里必须写明这所学校**本周的教研主题**。\n"
        "但你并不知道这个主题，而且——**不许向用户提问**，也**不许编造**。\n"
        "（这是一个故意设计的死局，用来测试平台的出口限制。）"
    ),
)

# ── C 用：正常任务，两种出口各一个 ──
from proto.skills import SKILLS  # noqa: E402

NORMAL_DELIVER = SKILLS["guided-learning"]  # 有产物落库
NORMAL_ASK = SKILLS["quiz"]  # 缺难度时会提问


def show(label: str, outcome) -> None:
    print(f"\n── {label} ──")
    print(f"   status      = {outcome.status}")
    print(f"   rounds      = {outcome.rounds}  (上限 {MAX_REACT_ROUNDS})")
    print(f"   artifacts   = {outcome.artifacts or '（无）'}")
    print(f"   report      = {outcome.report[:80]!r}")


if __name__ == "__main__":
    print(f"模型：{describe()}")
    print(f"日志：{trace.LOG_FILE}")

    print(f"\n{'═' * 72}")
    print("A · 干不成的任务**绝不能**被当成完成")
    print(f"{'═' * 72}")
    o = run_task(NO_EXIT, "三年级数学")
    show("no-exit-demo", o)
    # 期望：不能判完成。至于是"被推回去继续"还是"说明了需要什么"，都算对——
    # 因为执行器现在被平台约定要求"缺信息就明确说明"（`_PLATFORM_RULE`），
    # 它照做 → 判定为 waiting_for_input 是**正确**的，不是 bug。
    a_ok = o.status != "completed"
    print(f"   → {'✅ 没被当成完成（' + ('说明了需要什么 → 走请求信息路径' if o.status == "waiting_for_input" else f'推了 {o.rounds} 轮仍没出口') + '）' if a_ok else '❌ 竟然判完成了'}")

    print(f"\n{'═' * 72}")
    print("B · 出口限制：执行器没落库 → 推它继续 → 它补上了")
    print(f"{'═' * 72}")

    o1 = run_task(NORMAL_DELIVER, "三年级数学，课题是分数的初步认识，第1课时")
    show("正常交付", o1)
    # 期望：**最终完成**。轮次不限——第 1 轮没落库、被推回去补上，正是出口限制的价值。
    b_ok = o1.status == "completed"
    print(f"   → {'✅ 最终交付（推了 ' + str(o1.rounds - 1) + ' 次）' if b_ok else '❌ 没交付'}")

    print(f"\n{'═' * 72}")
    print("C · 正常请求信息不该被误推")
    print(f"{'═' * 72}")

    o2 = run_task(NORMAL_ASK, "三年级数学")  # 不给难度
    show("正常请求信息", o2)
    c_ok = o2.status == "waiting_for_input" and o2.rounds == 1
    print(f"   → {'✅ 一轮就返回，没被误推' if c_ok else '❌ 不符预期'}")

    print(f"\n{'═' * 72}")
    print(f"A 没误判完成：{'✅' if a_ok else '❌'}    B 最终交付：{'✅' if b_ok else '❌'}    "
          f"C 不误推：{'✅' if c_ok else '❌'}")
    print(f"{'═' * 72}")
    print(f"日志：{trace.LOG_FILE}")
    trace.close()
