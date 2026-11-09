"""单元级：**Task 的每个字段都要么渲染进交接书、要么在排除名单里**（不调 LLM）。

    uv run python -m evals.unit_briefing

**为什么要有它**（2026-10-29）：F15 的病根是"状态必须显式落在 Task 上"这条原则
**只靠人记得** —— 前两条原则（执行器无状态、重入走新 worker）是**结构性**的，
代码长成那样就自动满足；这一条是**纪律性**的，事故恰好出在它身上。

这条断言把它从"纪律"变成"机制"：造一个**所有字段都填满**的 Task，渲染，
逐个断言每个非排除字段的值都出现在结果里。**以后加字段忘了渲染，这里就红。**

> 有效性自检：把 `render_briefing` 里任意一块注释掉，本测试必须红
> （2026-10-29 手工验过：删掉 `progress` 那块 → 红。⚠️ 2026-11-05 判定退休，
> `progress`/`missing` 两个字段整条链摘掉了 —— 现在的自检对象是**别的段**）。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import ast
import asyncio
import io
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os

# 🔴 **单测的日志打 tag**（2026-11-06）：`logs/` 里真会话与测试垃圾混在一起，复盘时
#    grep 不动 —— 打上 tag 后文件名是 `proto-<ts>-unit-<名字>.log`，一眼可辨
#    （清理：`rm logs/*-unit-*.log`）。复用跑批那个 `PROTO_LOG_TAG`（`evals/_child.py`）。
# ⚠️ **必须排在所有非标准库 import 之前**：`proto.trace` 在 import 那一刻读 env 定文件名，
#    而 `from app import chat` 这种**间接** import 也会把它拉进来（实测踩过：
#    第一版插在 `from app import chat` 之后 → 日志照样没 tag）。
#    守卫：`unit_feed_waiting` ⑮。
os.environ["PROTO_LOG_TAG"] = "unit-briefing"
os.environ["PROTO_LOG_DIR"] = "tmp/logs"   # 🔴 测试日志是**噪音** —— 别混进给人看的 `logs/`

from proto.handoff import BODY, SECTION
from proto.handoff.briefing import _flat, _one_line_limit, render_briefing
from proto import artifacts
from proto.artifacts import store_artifact


def _declared_fields() -> list[str]:
    """`briefing.Task` 声明的字段（AST，不看注释）。"""
    src = io.open("proto/handoff/briefing.py", encoding="utf-8").read()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.ClassDef) and n.name == "Task":
            return [s.target.id for s in n.body if isinstance(s, ast.AnnAssign)]
    return []


def _built_fields() -> list[str]:
    """**建 Task 那个字面量**的键（AST）—— 现在住 `agent._new_task` 的 `return {…}`。

    ⚠️ **2026-11-09 挪窝**：`delegate` 拆成四个件之后，那个 `rec: Task = {…}`
    从 `delegate` 里搬进了 `_new_task`，形状也从**赋值**变成**返回**。
    ⇒ 锚点跟着改成"`_new_task` 里那个 `return` 的 dict 字面量"。
    ⚠️ 认的是**形状 ＋ 那个函数名**，不是行号 —— 再挪窝它会红（**不静默放过**），
    那时**改这里**，别去改产品代码迁就守卫。
    """
    src = io.open("proto/agent.py", encoding="utf-8").read()
    for fn in ast.walk(ast.parse(src)):
        if not (isinstance(fn, ast.FunctionDef) and fn.name == "_new_task"):
            continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict):
                return [k.value for k in n.value.keys if isinstance(k, ast.Constant)]
    return []

#: **显式排除名单** —— 这些字段不进交接书，每一条都要有理由。
#: ⚠️ 加字段的人只有两条路：渲染它，或者在这里写清楚为什么不渲染。
EXCLUDED: dict[str, str] = {
    "id": "平台内部标识（台账用），执行器不需要",
    "skill": "技能名 —— 技能指令已经整份注入了，名字没有增量信息",
    "stage": "平台状态机内部字段；'这是重入'这层意思由【续跑提示】承载",
    "agent_tried": "布尔标记（只写不读，跑批报告用）；它要传达的信息由 `qa` 里 `by=\"主 agent\"` 那条承载",
    "run_by": "平台内部字段（**台账**用它决定'全文在哪'，见 `ledger._SRC`），执行器不需要",
    "fault": "**平台故障的原因** —— 它是**平台**的事（台账据此说「平台故障，原样再派」、"
             "回执有【平台故障】段）。执行器是**隔离上下文、用完即弃**：它上一次有没有跑成，"
             "跟这一次该怎么干**无关**（重入时它看到的是完整交接书），写进去只是噪音",
    "fault_kind": "**平台故障的『类』**（`FaultKind`）—— 台账据此选引导句"
                  "（『原样再委派』 vs 『换措辞/改内容』，见 `ledger.FAULT_MARK`）；"
                  "执行器不需要（同 `fault`：上一次为什么没跑成，与这一次怎么干无关）",
    "asked": "**它申报要的那句原话** —— 服务的是**平台 → 用户**那一跳（透传贴它、将来弹框"
             "用它，见 `solution.md` §0.65 Y1），**执行器不需要再看一遍**：那次问答已经在"
             "【问答经过】里成对出现了（问 + 答，`qa[i].q` 就是收下答案那一刻的 `asked`），"
             "再贴一遍是同一个东西两个位置。"
             "⚠️ 2026-11-04 之前这句理由是**假的**：当时「问」取的是 `report`（那段过场），"
             "`asked` 在交接书里其实一次都没出现过。现在两处都取 `asked`，这条排除才成立",
}

#: 通用循环**查不了**的字段 —— 由下面的专门分块断言（不是排除，是换一种查法）
SPECIAL: dict[str, str] = {
    "user_turns": "嵌套结构（每轮 {n, text}）；而且**索引里只放一行开头**（正文按需取），"
                  "通用循环要求「值全文出现」，对它是反的 —— 见 ④",
    "qa": "嵌套结构（每对 {by, q, a}）；而且**要断的是「问和答成对出现」**，"
          "不是「每个字段值都出现」—— 见 ⑦",
}

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail else ""))


def _full_task() -> dict:
    """**所有字段都填满**的 Task —— 空字段测不出"漏渲染"。"""
    artifacts.clear()
    produced = store_artifact("《白桦》导学单", "课前预习用填空式导学单", "md", "导学单正文" * 40)
    readable = store_artifact("《白桦》教案", "四年级下册诗歌教学", "md", "教案正文" * 60)
    return {
        "id": "t12345678",
        "skill": "guided-learning",
        "task": "生成《白桦》导学单，课前预习用",
        "stage": "waiting_for_input",
        "artifacts": [produced],
        "refs": [readable],
        "report": "好的，我先设计学习目标……请问这份导学单用在哪？",
        "qa": [
            {"by": "用户", "q": "这份导学单用在哪？", "a": "课前预习"},
            {"by": "主 agent", "q": "学生填写形式你偏好哪一种？A. 填空补白 B. 表格记录",
             "a": "填空补白"},
        ],
        "agent_tried": True,
        # ⚠️ 故障和"待答"实际不会同时出现（故障的 stage 是 `failed`）—— 但 fixture 的
        #    规矩是**每个字段都填满**（空字段测不出"漏渲染"），而且 ② 那条"没泄漏"的断言
        #    要求值**非空**（空串在任何文本里都能"找到"）。填一个一眼能认出来的值。
        "fault": "TimeoutError: （fixture）这条不该出现在交接书里",
        # 故障的"类"（`FaultKind`）—— 台账用它选引导句；同样不该进交接书（见 EXCLUDED）。
        "fault_kind": "transient",
        "asked": "（fixture）它申报的那句原话 —— 也不该出现在交接书里",
        # 这轮是谁起的 —— 台账据此说"全文在哪"（`delegate` 有回执 / `feed` 没有）
        "run_by": "delegate",
        # 🔴 平台**不挑**哪几轮给全文（2026-11-03）：每一轮都只进**索引**一行
        # （序号 · 字数 · 开头），全文按需 `read_user_turn`。
        # ⚠️ 这三轮都**长于 40 字** —— 否则"开头"就是全文，④ 的负向断言测不出东西。
        "user_turns": [
            # 第 1 轮前面没有 AI 消息 → after 为空（渲染时那一行不该出现）
            {"n": 1, "text": "帮我给三年级语文《荷花》备一节课，出教案。人教版三年级下册，"
                             "第一课时，探究型上法，学生基础一般。直接生成，不用再问我。",
             "after": ""},
            # 🔴 短应答的语境：这一段全部意义就是**对冲转述失真** —— 见 ⑦
            {"n": 2, "text": "导学单用在课前预习，填空形式；网页是给三年级学生看的，别太深。"
                             "另外导学单和网页的内容都要参考教案。",
             "after": "好的，那我先确认两件事：这份导学单用在哪？学生填写形式你偏好哪一种？"},
            {"n": 3, "text": "再补一句：教案里的比喻句部分要多留一点时间，学生基础一般，"
                             "常见卡点是比喻句的理解，需要搭支架。",
             "after": "收到，我这就按你说的调整。"},
        ],
    }


def main() -> None:
    task = _full_task()
    text = render_briefing(task)

    print("① 每个非排除字段的值都要出现在交接书里")
    for key, value in task.items():
        if key in EXCLUDED or key in SPECIAL:
            continue
        values = value if isinstance(value, list) else [value]
        missing = [v for v in values if str(v) not in text]
        check(f"`{key}` 渲染了", not missing, f"没出现：{missing}" if missing else "")

    # 🔴 **覆盖性守卫**（2026-11-05 加）：`_full_task()` 必须**包含 `Task` 的每一个字段** ——
    #    不然上面那条规矩（"加字段的人只有两条路：渲染它，或者写明为什么不渲染"）**是空的**：
    #    新字段不进 fixture，① 和 ② **都看不见它**，静默漏掉（加 `fault` 时实测就是这样）。
    from typing import get_type_hints

    from proto.handoff.briefing import Task

    _missing = sorted(set(get_type_hints(Task)) - set(task))
    check("`_full_task()` 覆盖 `Task` 的每个字段（漏了 → ① ② 都查不到它）",
          not _missing, f"fixture 里没有：{_missing} —— 要么补进 fixture 让它被渲染，"
                        f"要么写进 EXCLUDED 说明为什么不")

    print("\n② 排除名单里的字段**不该**出现（双向：要么渲染、要么显式排除）")
    for key in EXCLUDED:
        value = str(task[key])
        # `stage` 的值是 waiting_for_input，正文里不该出现这个英文状态名
        ok = value not in text
        check(f"`{key}` 没泄漏", ok, "" if ok else f"出现了：{value!r}")

    print("\n②′ 产物**取不到**时不许静默跳过（R6：与回执同一副面孔）")
    # 🔴 2026-11-04 复审 R6：原来 `_index` 用 `if (a := …)` 把取不到的 ID 悄悄删掉 ——
    #    同一份坏数据，执行器看到的是"少了一行"，主 agent 在回执里看到的是"这行有问题"。
    dangling = {**task, "artifacts": ["art_999"], "refs": ["art_998"]}
    dt = render_briefing(dangling)
    check("产出的产物：缺失**显式写出**（不是少一行）",
          "art_999（**产物库里没有这一条**）" in dt, repr([ln for ln in dt.splitlines() if "art_9" in ln]))
    check("可读产物：同样显式写出",
          "art_998（**产物库里没有这一条**）" in dt, repr([ln for ln in dt.splitlines() if "art_9" in ln]))

    print("\n③ 重入该有的提示语都在")
    check("说了不要重复已完成的工作", "不要重复已完成的工作" in text)
    check("说了冲突以后面的为准", "以后面的为准" in text)
    check("说了在已有产物上补全", "在它基础上补全" in text)

    print("\n④ 用户原话：**每一轮都在索引里**（一行），正文**一律按需**")
    # 🔴 用户 2026-11-03：不再由主 agent 点名（"哪几轮相关"是**读的时候**才成立的关系，
    # 而编号只活在平台内部 —— 实测就数错了）。平台只保证两件确定的事：
    # ① 每一轮都在索引里；② 索引那一行**一字不改**地搬原话的开头。
    # 常驻成本 ∝ **轮次数**，与文本量无关 —— 用户粘 5 万字，这一层也只涨一行。
    for t in task["user_turns"]:
        head = " ".join(t["text"].split())[:_one_line_limit]
        listed = f"第 {t['n']} 轮（{len(t['text'])} 字）" in text and head in text
        check(
            f"第 {t['n']} 轮在索引里（序号 + 字数 + 开头）",
            listed,
            "" if listed else f"没找到：第 {t['n']} 轮 / {head!r}",
        )
        # 🔴 **正向保证**：超出一行的正文**不该**进交接书（它是按需那一层的事）
        if len(" ".join(t["text"].split())) > _one_line_limit:
            check(f"第 {t['n']} 轮超一行的部分**没进上下文**", t["text"] not in text)
    check("说了这是事实来源、冲突以原话为准", "以原话为准" in text)
    check("说了要看全文用 read_user_turn", "read_user_turn" in text)
    # 索引层的成本**只跟轮次数走**：短轮次（一行装得下）就是全文，一个字都不丢
    short = {"n": 9, "text": "讲解示范", "after": ""}
    t2 = render_briefing({**task, "user_turns": [short]})
    check("短轮次（一行装得下）→ 索引行**就是全文**", "第 9 轮（4 字）：讲解示范" in t2)

    print("\n⑤ 来源标注：**哪句是用户说的、哪句是模型说的**，一眼分得清")
    check("【任务】标明是主 agent 交代的", "主 agent 交代的" in text)
    check("【用户原话】标明是用户说的", "**用户说的**" in text)
    check("【问答经过】逐条标了**谁**答的",
          "（**用户**）" in text and "（**主 agent**）" in text)
    # 2026-11-03 分家之后，每段**只留一方的话**，来源由段头一次写死：
    #   【问答经过】＝ 执行器自己的话（问）＋ 谁答的（答）
    # ⚠️ 原来这里还有一条【上次判定】（第三方评审的话）—— 2026-11-05 判定退休，整段删掉。
    check("【问答经过】标明「问」是执行器的原话", "（执行器原话）" in text)

    print("\n⑦ 一问一答：**答不能脱离问**（用户 2026-11-02 定的形状）")
    # ① 每一对都必须"问在前、答在后"地出现 —— 只出现答案等于没交接上下文
    # ⚠️ 顺序只在**这一段内**比：`课前预习` 这类词在【用户原话】里也会出现，
    #    用全文 `find` 会拿到更早的那次命中（实测踩过，测试自己误报）
    # ⚠️ **段名从 `SECTION` 取**，不写死字面量 —— 2026-11-03 段名一改，写死的地方就静默失效了
    seg = text[text.find(SECTION["answered"]):text.find(SECTION["already_produced"])]
    check("【问答经过】这一段渲染出来了", bool(seg.strip()))
    # ⚠️ **按压平后的口径比**：多行值渲染时会整块缩进（`body_lines`），
    #    原文的多行字符串就不再是连续子串了 —— 用 `_flat` 两边同一个口径（同 `_flat` 的注释）
    flat = _flat(seg)
    for i, x in enumerate(task["qa"], 1):
        q, a = _flat(x["q"]), _flat(x["a"])
        both = q in flat and a in flat
        ordered = flat.find(q) < flat.find(a) if both else False
        check(f"第 {i} 对：问和答都在，且问在答前", both and ordered,
              "" if (both and ordered) else f"q={q!r} a={a!r}")
        check(f"第 {i} 对：标了**谁**答的（{x['by']}）", f"**答 {i}**（**{x['by']}**）" in seg)
    # ② 用户原话那一段：短应答必须带上"它在回答的上一句"
    for t in task["user_turns"]:
        after = str(t.get("after") or "").strip()
        head = " ".join(after.split())[:40]
        if after:
            check(f"第 {t['n']} 轮带了「它在回答的上一句」", "它在回答的上一句" in text and head in text,
                  "" if head in text else f"没找到：{head!r}")
        else:
            check(f"第 {t['n']} 轮前面没有 AI 消息 → 不该凭空造上下文",
                  "它在回答的上一句" not in text.split(f"第 {t['n']} 轮")[1].split("第 ")[0])
    # ③ `read_user_turn` 给的是**整轮**（原话 ＋ 它在回答的那句），不是孤零零一句
    from proto.executor import _user_turn_reader
    reader = _user_turn_reader(task["user_turns"])
    got = asyncio.run(reader.ainvoke({"turn": 2}))
    check("read_user_turn 返回原话全文", str(task["user_turns"][1]["text"]) in got)
    check("read_user_turn **也返回它在回答的那句**",
          "它在回答的上一句" in got and str(task["user_turns"][1]["after"]) in got)
    check("read_user_turn 对不存在的轮次仍然报错",
          "没有第 9 轮" in asyncio.run(reader.ainvoke({"turn": 9})))

    print("\n⑥ 首跑不该出现「续跑」相关的话（否则它会以为有前情）")
    fresh = {k: ([] if isinstance(v, list) else "") for k, v in task.items()}
    fresh.update({"task": "生成《白桦》导学单", "stage": "running", "id": "t1", "skill": "s"})
    fresh_text = render_briefing(fresh)
    # ⚠️ **不逐字钉标题**（标题带来源标注，措辞会改）—— 只断"**只有【任务】这一段**"
    check(
        "首跑只有【任务】一段",
        fresh_text.strip().startswith("【任务】") and fresh_text.count("【") == 1,
        repr(fresh_text),
    )
    check("首跑没有续跑提示", "续跑提示" not in fresh_text)

    print("\n⑧ 渲染出来的段都在 `HOPS['briefing']` 里声明过（和回执那边对称）")
    from proto.handoff import HOPS

    used = {k for k, v in SECTION.items() if v in text}
    declared = set(HOPS["briefing"])
    check("渲染出的段都在 `HOPS['briefing']` 里声明过", used <= declared,
          f"没声明：{sorted(used - declared)}")
    check("声明的段**都用到了**（没有死段）", declared <= used,
          f"声明了没人用：{sorted(declared - used)}")

    # ══════════════════════════════════════════════════════════════════════
    print("\n⑨ 去重：**最后一问 == report** 时只贴一遍（确定性 ＋ 必须标出来）")
    # 🔴 为什么这条能当**不变量**断，而不是"碰巧"：
    #    `qa` 里的「问」是**当时那条 `report`** 原样抄下来的
    #    （`graph._feed_waiting` / `agent.delegate` 两处都这么建），
    #    而 `report` 要到 `absorb_outcome` 才被覆盖 —— **渲染发生在覆盖之前**。
    #    所以"最后一问 == report"在**每一次重入渲染时恒成立**（实测 R07：白贴 275 字）。
    base = {
        "task": "生成导学单", "stage": "waiting_for_input",
        "user_turns": [],
        "artifacts": [], "refs": [],
    }
    # 多行的问（真实形状：执行器的整条 report，前面还有一句交代）
    q2 = ("好的，场景确认为 **A. 课前预习** ✅\n\n"
          "**第二个问题：你希望导学单以什么形式让学生填写？**\n"
          "- A. 填空补白\n- B. 表格记录")
    dup = {**base, "report": q2,
           "qa": [{"by": "用户", "q": "用在哪？", "a": "课前预习"},
                  {"by": "用户", "q": q2, "a": "A"}]}
    dup_text = render_briefing(dup)
    check("最后一问和 `report` 逐字相同时，**只出现一遍**",
          _flat(dup_text).count(_flat(q2)) == 1,
          f"出现了 {_flat(dup_text).count(_flat(q2))} 次")
    check("折叠**标出来了**（读的人分得清「它没说」和「被折叠了」）",
          "不重复贴" in dup_text)
    # 🔴 **反向断言**：不逐字相同时，**一个字都不能少** ——
    #    折叠写歪了会**静默吞掉**内容，那是比重复更贵的错（交接三问①）
    diff = {**base, "report": "好的，两个问题都确认了，现在进入第三步：生成导学单。",
            "qa": [{"by": "用户", "q": q2, "a": "A"}]}
    diff_text = render_briefing(diff)
    check("最后一问和 `report` 不同时，两句**都在**",
          diff["report"] in diff_text and _flat(q2) in _flat(diff_text))
    check("没折叠就不该出现「不重复贴」", "不重复贴" not in diff_text)
    check("首跑（无 qa 无 report）不出现【问答经过】",
          SECTION["answered"] not in render_briefing({**base, "task": "x"}))

    print("\n⑩ 排版：**段名独占一行 · 段体 2 列 · 续行不掉回 0 列**")
    # 🔴 **段名那一行只能是段名**（用户 2026-11-03 在三选一里挑的排法）：
    # 原来是 `【任务】（**主 agent 交代的** —— …）`，一长串括号说明跟段名挤在一行 ——
    # 读的人看着**像同一个层级**（原话："仿佛是同一个层级"）。来源现在另起一行。
    # ⚠️ 断言对象是**渲染结果**，换实现也照样管用。
    for label, t in (("满字段的 Task", task), ("重入（含折叠）", dup), ("兜底那条路", diff)):
        stray = [ln for ln in render_briefing(t).splitlines()
                 if ln.startswith("【") and ln.strip() not in SECTION.values()]
        check(f"{label}：段名独占一行（后面不跟内容）", not stray, f"越界的行：{stray[:2]}")
    # 🔴 这条是**结构性**的：它一次盖住实测那两种破法 ——
    #   ① 多行的「答」续行掉到 0 列；② 「答」和「问」的续行同缩进（靠编号区分）。
    #   （原来还有 ③ 点名轮的原文顶在 0 列 —— 已随"点名"一起消失，2026-11-03。）
    #   任何一种复发，这里当场红。
    # ⚠️ 断言对象是**渲染结果**，不是源码写法 —— 换实现也照样管用。
    for label, t in (("满字段的 Task", task), ("重入（含折叠）", dup), ("兜底那条路", diff)):
        bad = [ln for ln in render_briefing(t).splitlines()
               if ln.strip() and not ln.startswith(("【", "  "))]
        check(f"{label}：没有掉回 0 列的续行", not bad, f"越界的行：{bad[:3]}")
    # 多行的「答」整块缩进 —— 这是上面那条最常被踩的具体形状，单独钉一遍
    multiline_a = {**base, "report": "它最后说的。",
                   "qa": [{"by": "主 agent", "q": "问一句？", "a": "第一行\n第二行\n第三行"}]}
    ma_lines = {ln.strip(): ln for ln in render_briefing(multiline_a).splitlines()}
    check("多行的「答」续行**整块缩进**（不顶回 0 列）",
          all(ma_lines.get(k, "").startswith(BODY * 2) for k in ("第二行", "第三行")),
          repr([ma_lines.get(k) for k in ("第二行", "第三行")]))

    print("\n⑪ **建**（`delegate` 的字面量）和**声明**（`Task`）字段表必须一致")
    # 🔴 这一对原来**没有守卫**（2026-11-03 审核补）：①/② 管的是"声明 → 渲染"那一侧，
    #    而"建了没声明 / 声明了没建"没人管 —— 靠人眼对齐两张 12 项的清单。
    #    用 AST 抽两边的字段表直接对（不看注释、不跑代码）。
    declared, built = _declared_fields(), _built_fields()
    check("`Task` 声明的字段抽得到（AST）", bool(declared), "没抽到 —— 类名改了？")
    check("**建 Task 那个字面量**的字段抽得到（AST）", bool(built),
          "没抽到 —— `_new_task` 改名/挪窝了？改 `_built_fields()` 的锚点，别改产品代码迁就它")
    check("两边**字段集完全一致**", set(declared) == set(built),
          f"建了没声明：{sorted(set(built) - set(declared))}；"
          f"声明了没建：{sorted(set(declared) - set(built))}")

    print("\n⑫ 哑待答：新任务完成 ＋ 用户已另起话题 → 旧待答**显式取代**（F7 边界不动）")
    # 🔴 2026-11-06：用户没走 `↩` 答复通道、又发了新消息；新任务交付后旧待答永远挂着
    #    （每轮重摆、答它=重跑旧任务）—— 实测到过。
    #    判据是**两条结构事实**：① 新任务已完成；② 用户快照**前缀延长**（用户又说过话）。
    from proto.handoff.briefing import moot_older_waiting

    def _t(tid: str, *, skill: str = "s", stage: str = "waiting_for_input",
           turns: int = 1) -> dict:
        return {"id": tid, "skill": skill, "stage": stage,
                "user_turns": [{"n": i, "text": f"第{i}轮", "after": ""}
                               for i in range(1, turns + 1)]}

    done = _t("t-new", stage="completed", turns=2)   # 用户又说过话之后派的新任务，已完成
    old = _t("t-old", turns=1)                        # 更早的待答（快照停在 1 轮）
    mooted = moot_older_waiting(done, [old, done])
    check("旧待答（用户又说过话 ＋ 新任务完成）→ 被取代",
          [x["id"] for x in mooted] == ["t-old"] and mooted[0]["stage"] == "superseded",
          repr(mooted))
    check("返回的是**副本** —— 原 dict 一个字段都不动", old["stage"] == "waiting_for_input")
    # 🔴 **F7 回归守卫**：同一轮里重派（快照没延长）→ 一个字都不许碰 ——
    #    那正是"主 agent 自己编了答案重派、用户从没被问过"的场景，问题必须留着。
    check("F7：快照没延长（同一轮重派）→ **不取代**",
          moot_older_waiting(_t("t-guess", stage="completed", turns=1), [_t("t-old")]) == [])
    check("不同技能 → 不动",
          moot_older_waiting(_t("x", skill="a", stage="completed", turns=2),
                             [_t("y", skill="b")]) == [])
    check("更早的已经不是待答（completed / failed）→ 不动",
          moot_older_waiting(done, [_t("t-old", stage="completed")]) == []
          and moot_older_waiting(done, [_t("t-old", stage="failed")]) == [])
    check("更晚的待答（快照更长）→ 不动",
          moot_older_waiting(_t("x", stage="completed", turns=1), [_t("y", turns=2)]) == [])
    check("没有快照的（fixture / 手写 state）→ 说不清就不动",
          moot_older_waiting(done, [{"id": "t-fix", "skill": "s",
                                     "stage": "waiting_for_input"}]) == [])
    check("自己（同 id）→ 不动", moot_older_waiting(done, [done]) == [])

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
