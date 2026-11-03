"""单元级：**Task 的每个字段都要么渲染进交接书、要么在排除名单里**（不调 LLM）。

    uv run python -m evals.unit_briefing

**为什么要有它**（2026-10-29）：F15 的病根是"状态必须显式落在 Task 上"这条原则
**只靠人记得** —— 前两条原则（执行器无状态、重入走新 worker）是**结构性**的，
代码长成那样就自动满足；这一条是**纪律性**的，事故恰好出在它身上。

这条断言把它从"纪律"变成"机制"：造一个**所有字段都填满**的 Task，渲染，
逐个断言每个非排除字段的值都出现在结果里。**以后加字段忘了渲染，这里就红。**

> 有效性自检：把 `render_briefing` 里任意一块注释掉，本测试必须红
> （2026-10-29 手工验过：删掉 `progress` 那块 → 红）。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from proto.handoff.briefing import render_briefing
from proto.artifacts import ARTIFACTS, store_artifact

#: **显式排除名单** —— 这些字段不进交接书，每一条都要有理由。
#: ⚠️ 加字段的人只有两条路：渲染它，或者在这里写清楚为什么不渲染。
EXCLUDED: dict[str, str] = {
    "id": "平台内部标识（台账用），执行器不需要",
    "skill": "能力名 —— 技能指令已经整份注入了，名字没有增量信息",
    "stage": "平台状态机内部字段；'这是重入'这层意思由【续跑提示】承载",
    "agent_tried": "布尔标记（只写不读，跑批报告用）；它要传达的信息由 `qa` 里 `by=\"主 agent\"` 那条承载",
}

#: 通用循环**查不了**的字段 —— 由下面的专门分块断言（不是排除，是换一种查法）
SPECIAL: dict[str, str] = {
    "user_turns": "嵌套结构（每轮 {n, text}）；而且**只有开头几十个字该进交接书**（渐进式披露），"
                  "通用循环要求「值全文出现」，对它是反的 —— 见 ⑤",
    "based_on": "数字列表；通用循环里 `str(1) in text` 恒真，查了等于没查 —— 见 ⑤",
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
    ARTIFACTS.clear()
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
        "progress": "已完成学习目标与预习任务",
        "missing": ["当堂检测（需要课时才能写）"],
        "qa": [
            {"by": "用户", "q": "这份导学单用在哪？", "a": "课前预习"},
            {"by": "主 agent", "q": "学生填写形式你偏好哪一种？A. 填空补白 B. 表格记录",
             "a": "填空补白"},
        ],
        "agent_tried": True,
        # 第 1 轮**点名**了（主 agent 说"这次工作依据的就是它"）→ 交接书里给**全文**
        # 第 2/3 轮没点名 → 只给**开头几十个字**（全文按需 `read_user_turn`）
        # ⚠️ 没点名的这两轮必须**长于 40 字**，否则"开头"就是全文，⑤ 的负向断言就测不出东西
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
        "based_on": [1],
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

    print("\n② 排除名单里的字段**不该**出现（双向：要么渲染、要么显式排除）")
    for key in EXCLUDED:
        value = str(task[key])
        # `stage` 的值是 waiting_for_input，正文里不该出现这个英文状态名
        ok = value not in text
        check(f"`{key}` 没泄漏", ok, "" if ok else f"出现了：{value!r}")

    print("\n③ 重入该有的提示语都在")
    check("说了这是上一轮的情况", "上一轮的情况" in text)
    check("说了不要重复已完成的工作", "不要重复已完成的工作" in text)
    check("说了冲突以后面的为准", "以后面的为准" in text)
    check("说了在已有产物上补全", "在它基础上补全" in text)

    print("\n④ 用户原话：清单逐轮出现、点名的给全文、**没点名的不给全文**")
    picked = set(task["based_on"])
    for t in task["user_turns"]:
        head = " ".join(t["text"].split())[:40]
        listed = f"第 {t['n']} 轮" in text and head in text
        check(
            f"第 {t['n']} 轮在清单里（轮次 + 开头）",
            listed,
            "" if listed else f"没找到：第 {t['n']} 轮 / {head!r}",
        )
        if t["n"] in picked:
            check(f"第 {t['n']} 轮**点名了 → 给了全文**", t["text"] in text)
        else:
            # 🔴 这条是**渐进式披露的正向保证**：没点名的轮次，全文**不该**进交接书
            check(f"第 {t['n']} 轮没点名 → **没给全文**", t["text"] not in text)
    check("说了这是事实来源、冲突以原话为准", "以原话为准" in text)
    check("说了要看全文用 read_user_turn", "read_user_turn" in text)

    print("\n⑤ 来源标注：**哪句是用户说的、哪句是模型说的**，一眼分得清")
    check("【任务】标明是主 agent 交代的", "主 agent 交代的" in text)
    check("【用户原话】标明是用户说的", "**用户说的**" in text)
    check("【已经答复过的信息】逐条标了**谁**答的",
          "**用户** 答：" in text and "**主 agent** 答：" in text)
    # 这一段里混着**两方**的话，子段必须各自标来源（判定是第三方评审，执行器是当事人）
    check(
        "【你上次说到哪】子段标了来源",
        "**判定说**" in text and "**你上次说**" in text,
        "缺「判定说」或「你上次说」",
    )

    print("\n⑦ 一问一答：**答不能脱离问**（用户 2026-11-02 定的形状）")
    # ① 每一对都必须"问在前、答在后"地出现 —— 只出现答案等于没交接上下文
    # ⚠️ 顺序只在**这一段内**比：`课前预习` 这类词在【用户原话】里也会出现，
    #    用全文 `find` 会拿到更早的那次命中（实测踩过，测试自己误报）
    seg = text[text.find("【已经答复过的信息】"):text.find("【已经产出的产物】")]
    check("【已经答复过的信息】这一段渲染出来了", bool(seg.strip()))
    for i, x in enumerate(task["qa"], 1):
        q, a = str(x["q"]), str(x["a"])
        both = q in seg and a in seg
        ordered = seg.find(q) < seg.find(a) if both else False
        check(f"第 {i} 对：问和答都在，且问在答前", both and ordered,
              "" if (both and ordered) else f"q={q!r} a={a!r}")
        check(f"第 {i} 对：标了**谁**答的（{x['by']}）", f"**{x['by']}** 答：" in seg)
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
    got = reader.invoke({"turn": 2})
    check("read_user_turn 返回原话全文", str(task["user_turns"][1]["text"]) in got)
    check("read_user_turn **也返回它在回答的那句**",
          "它在回答的上一句" in got and str(task["user_turns"][1]["after"]) in got)
    check("read_user_turn 对不存在的轮次仍然报错", "没有第 9 轮" in reader.invoke({"turn": 9}))

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

    print("\n⑦ 渲染出来的段都在 `HOPS['briefing']` 里声明过（和回执那边对称）")
    from proto.handoff import HOPS, SECTION

    used = {k for k, v in SECTION.items() if v in text}
    declared = set(HOPS["briefing"])
    check("渲染出的段都在 `HOPS['briefing']` 里声明过", used <= declared,
          f"没声明：{sorted(used - declared)}")
    check("声明的段**都用到了**（没有死段）", declared <= used,
          f"声明了没人用：{sorted(declared - used)}")

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
