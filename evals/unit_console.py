"""单元级：**终端播报层**（不调 LLM）—— 折行与分块不许把结构弄坏。

    uv run python -m evals.unit_console

用户 2026-11-03 实测：*"return 那块日志还是很乱"* / 交接书里 `【任务】` 的正文
**第二行掉回 0 列**。两件事都出在这个文件覆盖的那层（`evals/chat.py`）：
**内容一个字没丢，是显示把结构吃了** —— 而结构（哪几行属于哪一段）正是读日志的人
唯一能依靠的东西。

管四条：

**① 折行补回行首缩进** —— 段体缩进是**结构**（`handoff.BODY`：段名 0 列 / 段体 2 列 /
   每层 +2）。折行不补的话，长段体折出来只有第一行缩进、后面全顶到 0 列，
   看着像另一段。

**② 折行不丢字** —— 缩进是**故意重复**的，内容一个字都不能少、不能多。

**③ 折行按词边界** —— `read_user_turn` 折成 `read_us` / `er_turn`，读的人要自己拼，
   拷贝的人拼不回来（日志里的标识符正是拿来搜代码的）。

**④ 块里不混进装饰** —— `trace.section` 前后那两道 `═══` 不是内容；
   `_LIVE` 行超 400 字要**带截断标记**（交接三问②：切了不标是最糟的）。

> 有效性自检：把 `_wrap` 里那句 `out.append(lead + cur)` 改回 `out.append(cur)`，① 必须红。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from evals import chat

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail and not ok else ""))


#: 真实形状：段体缩进 + 超长中文（会被折）
_BODY = (
    "  生成人教版小学语文《荷花》这一课的教案。学科：小学语文，课题：荷花，"
    "教材版本：人教版。用户未明确指定年级，按人教版常规编排（三年级下册）处理；"
    "如需确认具体年级和课时要求再问我。"
)
#: 深层：段体 2 列 + 列表项 2 列
_DEEP = "      - **A. 探究发现型** —— 以学生自主阅读、发现语言之美为主，教师引导点拨"


def main() -> None:
    print("① 折行补回行首缩进 —— 续行不许掉回 0 列")
    for label, line in (("段体（缩进 2）", _BODY), ("列表项（缩进 6）", _DEEP)):
        lead = line[: len(line) - len(line.lstrip())]
        got = chat._wrap(line, 40)
        check(f"{label}：折出了多行（前提是它真的超宽）", len(got) > 1, f"只折出 {len(got)} 行")
        check(f"{label}：**每一行**都带原缩进",
              all(ln.startswith(lead) for ln in got),
              repr([ln[:8] for ln in got]))

    print("\n② 折行不丢字、不加字（缩进是故意重复的，内容不是）")
    for width in (5, 13, 40, 100):
        for line in (_BODY, _DEEP, "没有缩进的一句很长很长的话，用来对照。", ""):
            lead = line[: len(line) - len(line.lstrip())]
            body = line[len(lead):]
            got = chat._wrap(line, width)
            joined = "".join(ln[len(lead):] if ln.startswith(lead) else ln for ln in got)
            check(f"宽 {width}：内容逐字保留（{line[:10] or '空行'}…）", joined == body,
                  f"{joined!r} != {body!r}")

    print("\n③ 折行按**词边界** —— 标识符和代码段不许被拆")
    for frag in ("read_user_turn", "waiting_for_input", "SKILL.md"):
        got = chat._wrap(f"要用哪一份就用 `{frag}` 读，不要凭那几十个字猜。", 24)
        check(f"`{frag}` 没被拦腰截断", any(frag in ln for ln in got), repr(got))

    print("\n④ 块里不混进装饰；超长行切了要标")
    seen: list[str] = []
    real_emit = chat._emit
    chat._emit = lambda role, text, stamp=None: seen.append(text)  # type: ignore[assignment]
    try:
        st = {"block": ("decide", ["【试答】"], "t"), "section": "plat", "ts": "t",
              "kind": "", "calls": {}}
        chat._feed(st, "═" * 70)                       # 段分隔线
        check("`═══` 分隔线**不进块**", len(st["block"][1]) == 1, repr(st["block"][1]))
        chat._feed({**st, "block": None}, "[2026-11-03 12:00:00.000] run_task · R01 " + "长" * 500)
        check("`_LIVE` 超长行**带截断标记**", "平台截断" in seen[-1], seen[-1][-50:])
        seen.clear()
        chat._feed({**st, "block": None}, "[2026-11-03 12:00:00.000] run_task · R01 短")
        check("短行**不该**被标", "平台截断" not in seen[-1])
    finally:
        chat._emit = real_emit  # type: ignore[assignment]

    print("\n⑤ 工具返回的标签**不许跨段串**（2026-11-03 实测踩到）")
    # 真实形状：主 agent 调 `delegate` → 中间执行器那条链路调 `write_artifact`
    # → 回到主 agent 收 `delegate` 的返回。全局 FIFO 的话，执行器的返回会弹出
    # `delegate`、主 agent 的回执会弹出 `write_artifact` —— **两个标签整好对调**。
    seen2: list[str] = []
    chat._emit = lambda role, text, stamp=None: seen2.append(text)  # type: ignore[assignment]
    try:
        st2 = {"section": "", "kind": "", "ts": "t", "calls": {}, "block": None}
        for line in (
            "[2026-11-03 12:00:00.000] node_agent 第 1 步 消息轨迹（2 条）:",
            "  [1] AIMessage(20字)",
            "    tool_call=delegate args={'skill_name': 'lesson-prep'}",
            "[2026-11-03 12:00:01.000] run_task 第 1 轮 消息轨迹（3 条）:",
            "  [2] AIMessage(20字)",
            "    tool_call=write_artifact args={'title': '《荷花》教案'}",
            "  [3] ToolMessage(30字)",
            "    content='已写入产物 art_001（4931 字，md）'",
            "[2026-11-03 12:00:02.000] node_agent 第 2 步 消息轨迹（4 条）:",
            "  [4] ToolMessage(40字)",
            "    content='【交付】\\n  ✅ 已完成 —— 交付 1 个产物'",
        ):
            chat._feed(st2, line)
    finally:
        chat._emit = real_emit  # type: ignore[assignment]
    rets = [t for t in seen2 if "的返回" in t]
    check("执行器的返回标的是 `write_artifact`", len(rets) == 2 and rets[0].startswith("write_artifact"),
          repr([t.splitlines()[0] for t in rets]))
    check("主 agent 的返回标的是 `delegate`", len(rets) == 2 and rets[1].startswith("delegate"),
          repr([t.splitlines()[0] for t in rets]))

    print("\n⑥ 读全文的工具返回：**一个字都不贴**，只报多大一份、原文在哪")
    # 用户 2026-11-03：*"读取 skill 全文 你全显示没必要"* —— 原文另有出处
    # （技能包 / 产物落盘文件），贴一段既不是全文、又不是摘要。
    seen4: list[str] = []
    chat._emit = lambda role, text, stamp=None: seen4.append(text)  # type: ignore[assignment]
    try:
        for tool in ("read_skill", "read_skill_file", "read_artifact"):
            st4 = {"section": "agent", "kind": "ToolMessage", "ts": "t",
                   "calls": {"agent": [tool]}, "block": None}
            chat._feed(st4, "    content='" + "很长的正文。" * 200 + "'")
            got = seen4[-1]
            check(f"`{tool}` 的返回**不含正文**", "很长的正文" not in got, repr(got[:70]))
            check(f"`{tool}` 的返回**报了字数**", "1200 字" in got, repr(got[:80]))
    finally:
        chat._emit = real_emit  # type: ignore[assignment]

    print("\n⑦ repr 的**两种引号都要剥**（2026-11-03 实测踩到）")
    # 真实形状：回执正文里含 `read_artifact('art_001')` —— Python 的 repr 见内容里有单引号，
    # **改用双引号**；只剥 `'` 的话整份回执被一对多余的 `"` 包住。
    seen3: list[str] = []
    chat._emit = lambda role, text, stamp=None: seen3.append(text)  # type: ignore[assignment]
    try:
        st3 = {"section": "agent", "kind": "ToolMessage", "ts": "t",
               "calls": {"agent": ["delegate"]}, "block": None}
        chat._feed(st3, '    content="【交付】\\n  要看就用 `read_artifact(\'art_001\')` 读"')
    finally:
        chat._emit = real_emit  # type: ignore[assignment]
    got3 = seen3[-1]
    check("双引号包住的 repr **剥干净了**（没有多余的 `\"`）",
          not got3.splitlines()[1].startswith('"') and not got3.rstrip().endswith('"'),
          repr(got3[:60]))
    check("`\\n` 还原成了真换行", "\n" in got3 and "【交付】" in got3.splitlines()[1])

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

