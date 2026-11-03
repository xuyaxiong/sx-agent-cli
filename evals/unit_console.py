"""单元级：**终端播报层**（不调 LLM）—— 折行与分块不许把结构弄坏。

    uv run python -m evals.unit_console

用户 2026-11-03 实测：*"return 那块日志还是很乱"* / 交接书里 `【任务】` 的正文
**第二行掉回 0 列**。两件事都出在这个文件覆盖的那层（`evals/chat.py`）：
**内容一个字没丢，是显示把结构吃了** —— 而结构（哪几行属于哪一段）正是读日志的人
唯一能依靠的东西。

管这些：

**① 折行补回行首缩进** —— 段体缩进是**结构**（`handoff.BODY`：段名 0 列 / 段体 2 列 /
   每层 +2）。折行不补的话，长段体折出来只有第一行缩进、后面全顶到 0 列，
   看着像另一段。

**② 折行不丢字** —— 缩进是**故意重复**的，内容一个字都不能少、不能多。

**③ 折行按词边界** —— `read_user_turn` 折成 `read_us` / `er_turn`，读的人要自己拼，
   拷贝的人拼不回来（日志里的标识符正是拿来搜代码的）。

**④ 播报行带截断标记** —— 机制层摘要行超 `_LIVE_CAP` 要**带标记**截断
   （交接三问②：切了不标是最糟的）。

**⑤ 工具配对走 `tool_call_id`（结构）** —— 事件里带的是消息对象本身，
   配对不再靠"按段 FIFO"猜；同一段内乱序返回也能对上。

**⑥ 读全文的工具返回：一个字都不贴**，只报多大一份、原文在哪。

**⑦ 文本**原样**过桥** —— 事件带的是真字符串，repr 反转义那条 hack 已死：
   单引号、双引号、换行原样到屏。

**⑧ 块先关再播** —— 流式行不许插到挂着的块前面（否则读起来时序是反的）；
   试答的 PROMPT/RESPONSE 归并成**一块**（同一次 LLM 调用）。

**⑨ 切了一刀就得看得见** —— `cut` 事件进终端（标记落在**块的头**，不必长在切口上）。

**⑩ 角色只认 `role` 枚举** —— `who` / `tag` 是文案，改措辞不许影响任何逻辑。

**⑪ kind 是受控词汇** —— 打错当场炸；展示层对每个 kind 都得**表态**
   （上屏 / 专门分支 / 只进日志）。

**⑫ 一条语义、两层各自渲染** —— 谁都不许按**位置**切文本（`finalize: ` 前缀归记录层）。

> 有效性自检：把 `_wrap` 里那句 `out.append(lead + cur)` 改回 `out.append(cur)`，① 必须红。
> ⑤ 把 `_on_dump` 里的 `by_id` 配对改回"按出现顺序弹队列"，乱序用例必须红。
> ⑫ 把 `chat._on_event` 的 relay 分支改回 `[len("finalize: "):]`，⑫ 必须红。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import io
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from langchain_core.messages import AIMessage, ToolMessage

from proto import trace
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


def _tool_ai(name: str, args: dict, cid: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid}])


def _raises(fn) -> bool:
    """调一下，看它是不是**当场炸**（`ValueError`）。"""
    try:
        fn()
    except ValueError:
        return True
    return False


def _reset_emit(monkey):
    """把 `chat._emit` 换成捕获器，返回 (记录列表, 还原函数)。

    🔴 **截断走 `chat._cap_line`**（不是在这里重写一遍）：stub 自己抄一份的话，
    测的就是 stub，产品的截断坏掉了这条也照绿（2026-11-03 踩过：④ 永远红，
    因为旧 stub 把 `cap` 整个丢了）。
    """
    seen: list[tuple[str, str]] = []
    real = chat._emit

    def fake(role, text, stamp=None, *, cap=False):
        seen.append((role, chat._cap_line(text) if cap else text))

    chat._emit = fake  # type: ignore[assignment]
    chat._PENDING = None
    return seen, (lambda: setattr(chat, "_emit", real))


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

    print("\n④ 播报行超长**带截断标记**；短行不标")
    seen, restore = _reset_emit(chat)
    try:
        chat._on_event({"type": "section", "kind": "run_begin", "ts": "t",
                        "title": "run_task · R01 " + "长" * 500})
        check("超长行**带截断标记**", "平台截断" in seen[-1][1], seen[-1][1][-50:])
        chat._on_event({"type": "section", "kind": "run_begin", "ts": "t",
                        "title": "run_task · R01 短"})
        check("短行**不该**被标", "平台截断" not in seen[-1][1])
    finally:
        restore()

    print("\n⑤ 工具配对走 `tool_call_id` —— 跨段、同段乱序都不许串（旧 FIFO hack 的坟）")
    seen5, restore5 = _reset_emit(chat)
    try:
        # 跨段：主 agent 调 `delegate` → 执行器调 `write_artifact` → 各自的返回
        chat._on_event({"type": "dump", "role": trace.AGENT, "tag": "主 agent 第 1 步", "ts": "t", "messages": [
            _tool_ai("delegate", {"skill_name": "lesson-prep"}, "call_a"),
        ]})
        chat._on_event({"type": "dump", "role": trace.EXECUTOR, "tag": "run_task 第 1 轮", "ts": "t", "messages": [
            _tool_ai("write_artifact", {"title": "《荷花》教案"}, "call_b"),
            ToolMessage(content="已写入产物 art_001（4931 字，md）", tool_call_id="call_b"),
        ]})
        chat._on_event({"type": "dump", "role": trace.AGENT, "tag": "主 agent 第 2 步", "ts": "t", "messages": [
            ToolMessage(content="【交付】\n  ✅ 已完成 —— 交付 1 个产物", tool_call_id="call_a"),
        ]})
        rets = [t for r, t in seen5 if r == "ret" and "的返回" in t]
        check("执行器的返回标的是 `write_artifact`",
              len(rets) == 2 and rets[0].startswith("write_artifact"),
              repr([t.splitlines()[0] for t in rets]))
        check("主 agent 的返回标的是 `delegate`",
              len(rets) == 2 and rets[1].startswith("delegate"),
              repr([t.splitlines()[0] for t in rets]))
        # 同段乱序：两个调用、返回按 **id** 对 —— 顺序弹队列必错
        seen5.clear()
        chat._on_event({"type": "dump", "role": trace.EXECUTOR, "tag": "run_task 第 2 轮", "ts": "t", "messages": [
            _tool_ai("read_skill", {"skill_name": "x"}, "call_x"),
            _tool_ai("read_artifact", {"artifact_id": "art_001"}, "call_y"),
            ToolMessage(content="B 的返回", tool_call_id="call_y"),
            ToolMessage(content="A 的返回", tool_call_id="call_x"),
        ]})
        rets5 = [t.splitlines()[0] for r, t in seen5 if r == "ret"]
        # ⚠️ 这两个是**读全文的工具**（`_READ_TOOLS`），返回会被特判成"读了全文，N 字"、
        #    **不复述正文** —— 所以这里只校验**名字对得上**（见 ⑥）。
        check("乱序返回按 `tool_call_id` 对上",
              [t.split(" 的返回")[0] for t in rets5] == ["read_artifact", "read_skill"],
              repr(rets5))
    finally:
        restore5()

    print("\n⑥ 读全文的工具返回：**一个字都不贴**，只报多大一份、原文在哪")
    seen6, restore6 = _reset_emit(chat)
    try:
        for tool in ("read_skill", "read_skill_file", "read_artifact"):
            chat._on_event({"type": "dump", "role": trace.AGENT, "tag": "主 agent 第 3 步", "ts": "t", "messages": [
                _tool_ai(tool, {}, "call_t"),
                ToolMessage(content="很长的正文。" * 200, tool_call_id="call_t"),
            ]})
            got = seen6[-1][1]
            check(f"`{tool}` 的返回**不含正文**", "很长的正文" not in got, repr(got[:70]))
            check(f"`{tool}` 的返回**报了字数**", "1200 字" in got, repr(got[:80]))
    finally:
        restore6()

    print("\n⑦ 文本**原样**过桥 —— repr 反转义 hack 已死（事件带的是真字符串）")
    seen7, restore7 = _reset_emit(chat)
    try:
        raw = "【交付】\n  要看就用 read_artifact('art_001') 读，或 \"双引号\" 也在"
        chat._on_event({"type": "dump", "role": trace.AGENT, "tag": "主 agent 第 4 步", "ts": "t", "messages": [
            _tool_ai("delegate", {}, "call_z"),
            ToolMessage(content=raw, tool_call_id="call_z"),
        ]})
        got7 = seen7[-1][1]
        check("单引号 / 双引号 / 换行**原样保留**",
              got7 == f"delegate 的返回：\n{raw}", repr(got7[:80]))
    finally:
        restore7()

    print("\n⑧ 块**先关再播**；试答的 PROMPT/RESPONSE 归并成**一块**")
    seen8, restore8 = _reset_emit(chat)
    try:
        chat._on_event({"type": "block", "kind": "executor_briefing", "ts": "t",
                        "label": "发给执行器的内容（能力指令按名引用）",
                        "text": "【能力指令】lesson-prep\n\n【已知信息】\n  【任务】备课",
                        "indent": "  "})
        chat._on_event({"type": "stream", "kind": "", "ts": "t", "role": trace.EXECUTOR,
                        "who": "执行器·lesson-prep", "text": "好的，信息齐全"})
        check("注入块**在执行器开口之前**就播了（块先关）",
              len(seen8) == 2 and seen8[0][0] == "inject" and "【已知信息】" in seen8[0][1],
              repr([(r, t[:14]) for r, t in seen8]))
        check("流式行照播（没被块吞掉、也没丢）",
              seen8[-1] == ("say", "好的，信息齐全"), repr(seen8[-1]))
        # 试答：section(ask_main) 开块 → PROMPT / RESPONSE 归并 → 下一条事件关块
        seen8.clear()
        chat._on_event({"type": "section", "kind": "ask_main", "ts": "t", "title": "…"})
        chat._on_event({"type": "block", "kind": "llm_io", "ts": "t",
                        "label": "PROMPT（发给 LLM 的全文）", "text": "你的上下文全文…", "indent": "  "})
        chat._on_event({"type": "block", "kind": "llm_io", "ts": "t",
                        "label": "RESPONSE（0.1s）", "text": "can_answer=False", "indent": "  "})
        chat._on_event({"type": "stream", "kind": "", "ts": "t", "role": trace.AGENT,
                        "who": "主 agent", "text": "后面的话"})
        check("试答整块一次播出（PROMPT+RESPONSE 归并）",
              any(r == "decide" and "PROMPT" in t and "RESPONSE" in t and "can_answer=False" in t
                  for r, t in seen8), repr([(r, t[:20]) for r, t in seen8]))
        check("试答块**在后面的流式行之前**播（时序不倒）",
              [r for r, _ in seen8].index("decide") < [r for r, _ in seen8].index("agent"))
    finally:
        restore8()

    print("\n⑨ 切了一刀就得看得见 —— `cut` 事件进终端（交接三问②）")
    seen9, restore9 = _reset_emit(chat)
    try:
        chat._on_event({"type": "cut", "kind": "", "ts": "t", "where": "判定输入·材料",
                        "kept": 4000, "total": 5136, "see": "（判定没有工具循环）"})
    finally:
        restore9()
    check("`✂️ 截断` 进终端、且归机制层（plat）",
          len(seen9) == 1 and seen9[0][0] == "plat" and seen9[0][1].startswith("✂️ 截断")
          and "5136" in seen9[0][1],
          repr(seen9))

    print("\n⑩ 角色只认 `role` 枚举 —— **措辞怎么改都不影响**（2026-11-03 e2e 暴露的残余耦合）")
    seen10, restore10 = _reset_emit(chat)
    try:
        # ① 显示名 / tag **全是乱写的**，只要 role 对，角色就必须对
        chat._on_event({"type": "stream", "kind": "", "ts": "t", "role": trace.EXECUTOR,
                        "who": "随便一个名字", "text": "执行器说的话"})
        check("`who` 换成任意措辞，仍认作执行器（say）",
              seen10[-1] == ("say", "执行器说的话"), repr(seen10[-1]))

        seen10.clear()
        chat._on_event({"type": "dump", "role": trace.EXECUTOR, "tag": "完全不同的 tag",
                        "ts": "t", "messages": [_tool_ai("write_artifact", {}, "c1")]})
        check("`tag` 换成任意措辞，返回仍标「执行器」",
              any(r == "tool" and t.startswith("执行器 → write_artifact") for r, t in seen10),
              repr([(r, t[:20]) for r, t in seen10]))

        # ② 旧判据在这里**故意反过来**：who 写着"执行器"，role 说它是主 agent
        seen10.clear()
        chat._on_event({"type": "stream", "kind": "", "ts": "t", "role": trace.AGENT,
                        "who": "执行器·假的", "text": "其实是主 agent"})
        check("role 说了算：`who` 写着「执行器」也当主 agent",
              seen10[-1] == ("agent", "其实是主 agent"), repr(seen10[-1]))

        # ③ 角色缺失 / 非法 → **吼一声**，绝不静默当成主 agent
        seen10.clear()
        chat._on_event({"type": "stream", "kind": "", "ts": "t", "who": "x", "text": "没 role"})
        check("role 缺失 → 上屏警告（不静默降级）",
              any("未知角色" in t for _, t in seen10),
              repr([(r, t[:30]) for r, t in seen10]))
    finally:
        restore10()

    print("\n⑪ kind 是**受控词汇** —— 打错当场炸；展示层对每个 kind 都得表态")
    # 🔴 2026-11-03 审核 F3：kind 原来是自由字符串 —— 把 `dispatch` 打成 `delegate`
    # 或改个名，终端上那一行就**静默消失**，没有任何测试会红。
    for bad in ("delagate", "run_Begin"):
        for call in (lambda b=bad: trace.note("x", kind=b),
                     lambda b=bad: trace.section("x", kind=b),
                     lambda b=bad: trace.block("l", "x", kind=b)):
            try:
                call()
                raised = False
            except ValueError:
                raised = True
            check(f"`kind={bad}` → **当场炸**（不静默不上屏）", raised)
    check("`_EVENT_TYPES` 不是摆设：形状打错也当场炸",
          _raises(lambda: trace._publish({"type": "bogus", "ts": "t"})))
    known = set(chat._ROLE_BY_KIND) | set(chat._SPECIAL_KINDS) | set(chat._LOG_ONLY_KINDS)
    check("展示层对 `trace.KINDS` 的**每一个** kind 都表了态",
          known == set(trace.KINDS),
          f"没表态：{sorted(set(trace.KINDS) - known)}；表了不存在的：{sorted(known - set(trace.KINDS))}")

    print("\n⑫ 一条语义、两层各自渲染 —— 谁都不许按**位置**切文本")
    # 🔴 原来展示层写的是 `str(ev["text"])[len("finalize: "):]` —— 只改文案措辞，
    #    上屏就被**静默吃掉 10 个字**（2026-11-03 审核实测）。前缀归**记录层**（文件格式）。
    relay_text = "透传 2 条待答（平台直接贴，不判断主 agent 问没问）"
    seen12, restore12 = _reset_emit(chat)
    try:
        chat._on_event({"type": "note", "kind": "relay", "ts": "t", "text": relay_text})
        shown = seen12[-1][1] if seen12 else ""
        check("展示层**原样**上屏（措辞怎么改都不许被切）",
              seen12 and seen12[-1][0] == "ask" and relay_text in shown
              and "finalize:" not in shown, repr(shown[:70]))
    finally:
        restore12()
    before = trace.LOG_FILE.stat().st_size
    trace.note(relay_text, kind="relay")
    with io.open(trace.LOG_FILE, "rb") as f:
        f.seek(before)
        line = f.read().decode("utf-8", "replace").strip()
    check("记录层仍带 `finalize: ` 前缀（文件格式不变）",
          line.endswith("finalize: " + relay_text), repr(line[:80]))

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
