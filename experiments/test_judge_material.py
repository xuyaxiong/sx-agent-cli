"""验证判定的「实际执行结果」构造正确 —— **纯函数，不调 LLM**。

    uv run python -m experiments.test_judge_material

回归的是这个 bug：

    出口循环里消息是**累积**的（`messages = list(resp["messages"]) + [HumanMessage(nudge)]`），
    而判定原来用的是 `_last_ai_text(resp)` —— **只取最后一条 AI 消息**。
    于是第 2 轮只回了句总结时，第 1 轮的交付物判定**看不到**。

    实测日志里执行器自己写着「我没有从头再来，**继续接着上面的进展**」——
    而"上面"判定看不到。

现在判定用 `_judge_material()`：**产物正文 + 所有轮次的 AI 产出**，每项各自保头保尾。
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from langchain_core.messages import AIMessage, HumanMessage

from proto.executor import (
    JUDGE_ITEM_BUDGET,
    JUDGE_TOTAL_BUDGET,
    _clip,
    _judge_material,
)

PASS, FAIL = "✅", "❌"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL} {name}")
    if detail:
        print(f"      {detail}")


# ──────────────────────── ① _clip：保头保尾 + 显式标注省略 ────────────────────────

print("\n① _clip —— 保头保尾，中间显式标注省略了多少字")

short = "短文本，不该被动"
check("短文本原样返回", _clip(short) == short)

# ⚠️ 语料必须**长过 `JUDGE_ITEM_BUDGET`**，否则 `_clip` 原样返回、下面全部无意义。
# 原来写死 2200 字，预算从 800 提到 4000（2026-10-28）之后这条就一直是假失败。
long_text = "头" * 1500 + "中" * 2000 + "尾" * 1500  # 5000 字 > 预算
clipped = _clip(long_text)
check("超预算时变短", len(clipped) < len(long_text),
      f"{len(long_text)} 字 → {len(clipped)} 字")
# ⚠️ 标记的措辞是**被实测逼出来的**（见 `_clip` docstring）：写"省略"会被判定当成
# 作者占位符，于是产物越长越必然判 incomplete。所以这里断言的是当前契约的措辞。
check("**标注了截断**（比保头保尾本身更重要）", "平台截断" in clipped and "字" in clipped,
      repr(clipped[clipped.index("…") : clipped.index("…") + 24]))
check("头部保留", clipped.startswith("头" * 100))
check("尾部保留（最新状态）", clipped.endswith("尾" * 100))
check("截断字数正确", f"中间 {len(long_text) - JUDGE_ITEM_BUDGET} 字" in clipped)


# ──────────────────────── ② 回归：第 1 轮的交付物不能丢 ────────────────────────

print("\n② 回归 —— 第 1 轮产出交付物、第 2 轮只回总结")

DELIVERABLE = '```courseware-config\n{"grade": "三年级", "slides": 6}\n```'
SUMMARY = "好的，课件配置已经生成完毕，请查收。"

# 精确复刻出口循环累积出来的消息列表（executor.py 的 run_task 循环）
resp = {"messages": [
    HumanMessage("【能力指令】…【已知信息】…"),
    AIMessage(DELIVERABLE),                      # 第 1 轮：交付物
    HumanMessage("任务还没完成。请接着往下做。"),  # 推回去
    AIMessage(SUMMARY),                          # 第 2 轮：只说了句总结
]}

material = _judge_material([], resp)
check("第 1 轮的交付物**还在**（这就是原来的 bug）", "courseware-config" in material,
      f"material {len(material)} 字")
check("第 2 轮的总结也在", SUMMARY in material)
check("两段都标了序号", "【产出 1】" in material and "【产出 2】" in material)


# ──────────────────────── ③ 3 轮：中间那轮不能被挤掉 ────────────────────────

print("\n③ 3 轮 —— 每轮各自保头保尾，中间那轮不能被整体截断挤掉")

MID = "中间轮的独有标记MIDDLE_MARKER" + "中" * 2000   # 故意做长，逼它被截
resp3 = {"messages": [
    HumanMessage("…"),
    AIMessage("第一轮" + "一" * 500),
    HumanMessage("接着做"),
    AIMessage(MID),                              # 第 2 轮（落在"中间"位置）
    HumanMessage("接着做"),
    AIMessage("第三轮" + "三" * 500),
]}
m3 = _judge_material([], resp3)
check("3 段都在", m3.count("【产出") == 3, f"material {len(m3)} 字")
check("**中间那轮没被挤掉**（整体保头保尾会丢它）", "MIDDLE_MARKER" in m3)
check("第 3 轮（最新）也在", "第三轮" in m3)


# ──────────────────────── ④ 总量兜底 ────────────────────────

print("\n④ 总量兜底 —— 项数太多时砍后面，并显式说明少了几项")

# ⚠️ 每段必须够长，总长要**超过 `JUDGE_TOTAL_BUDGET`**（16000）才触发兜底。
# 原来写 900 字 × 8 = 7200 字，预算放大之后这条一直是假失败。
resp_many = {"messages": [AIMessage(f"第{i}段" + "字" * 2500) for i in range(1, 9)]}
mm = _judge_material([], resp_many)
check("总量不超预算太多", len(mm) <= JUDGE_TOTAL_BUDGET + 200, f"{len(mm)} 字")
check("**显式说明少了几项**", "未展示" in mm,
      mm[mm.index("……（另有") : mm.index("……（另有") + 30] if "……（另有" in mm else "（没有这行）")


# ──────────────────────── ⑤ 有产物：交付物是产物，不是散文 ────────────────────────

print("\n⑤ 有产物 —— **不能把散文喂给判定**（实测踩过的回归）")

from proto.skills import store_artifact  # noqa: E402

aid = store_artifact(
    "导学单", "课前预习用填空式导学单", "【导学单正文】学习目标 / 预习任务 / 当堂检测，内容齐全。"
)

resp_art = {"messages": [
    HumanMessage("…"),
    AIMessage("分数不可怕，导学单先来探路！"),
    AIMessage("导学单已生成并写入产物库，不过字数略超 150 字，如果需要进一步压缩，我马上帮你收紧。需要调整吗？"),
]}
ma = _judge_material([aid], resp_art)

check("产物正文在", "导学单正文" in ma)
check("**礼貌收尾「需要调整吗？」没被喂进去**", "需要调整吗" not in ma,
      "否则判定会把它当成「请求信息」→ 误判 waiting_for_input")
check("过程话术也没进去", "导学单先来探路" not in ma)


# ──────────────────────── ⑥ 空产出 ────────────────────────

print("\n⑥ 执行器什么都没说")

check("有兜底文案", "一句话都没说" in _judge_material([], {"messages": [HumanMessage("…")]}))


# ──────────────────────── 汇总 ────────────────────────

n_pass = sum(1 for _, ok, _ in results if ok)
n = len(results)
print(f"\n{'═' * 60}")
print(f"{'全部通过' if n_pass == n else '有失败'}：{n_pass}/{n}")
print(f"{'═' * 60}")
sys.exit(0 if n_pass == n else 1)
