"""单元级：**交接三问**（不调 LLM）。

    uv run python -m evals.unit_handoff

用户 2026-11-02 定的规矩，原话：

> "一定要注意交接的地方：**① 交接了什么内容　② 内容是否被截断了
>  ③ 过长的交接内容是否考虑了渐进式披露的原则** —— 这三点深刻影响 agent 执行效果。"

这个测试把它变成机制（不再是"记得就好"）：

**① 源码扫描** —— `proto/*.py` 里每一处切片（`[:N]` / `[-N:]`）**要么在豁免名单里写明理由，
要么就不许有**。以后谁再手滑加一个 `report[:150]`，这里当场红。
（和 `unit_briefing.py` 的 `EXCLUDED` 同构：**加字段只有两条路，渲染它，或者写明为什么不**。）

**② 逐交接点用超长输入验完整性** —— 每个交接渲染器喂一段"顶穿任何阈值"的输入，
断内容**一个字都没丢**；确实需要截的（台账那种每轮重复注入的索引），断它**标了**。

> 有效性自检：把 `render_briefing` 里 `report` 换成 `report[:200]`，② 必须红。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import io
import re
import sys
from glob import glob

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail and not ok else ""))


# ═══════════════════════════ ① 源码扫描 ═══════════════════════════

#: 切片形状：`[:N]`（切头）、`[-N:]`（切尾）。**不**包含 `[N:M]` / `[N:]`（那是取段、不是截断）。
SLICE = re.compile(r'\[\s*:\s*\d+\s*\]|\[\s*-\s*\d+\s*:\s*\]')

#: **豁免名单** —— 每一条都要有理由。键 = 文件名，值 = [(正则, 理由)]。
ALLOWED: dict[str, list[tuple[str, str]]] = {
    'proto/agent.py': [
        (r'tool_call_id', 'ID 派生（取尾部 8 位）—— 不是交接内容'),
        (r'q_lines\[0\]|q_lines\[1:\]', '按行拆分渲染，不丢字'),
        (r'based_on_turns=\[1, 3\]', '文档字符串里的示例'),
        (r'原来七个返回口各拼各的字符串', '文档字符串里**引用**旧写法说明为什么改 —— 不是真代码'),
    ],
    'proto/graph.py': [
        (r'text\[:limit\]', '**显式截断**：带【平台截断…】标记并指向回执 —— 见 `_brief`'),
    ],
    'proto/trace.py': [
        (r'%f"\)\[:-3\]', '时间戳砍到毫秒'),
    ],
    'proto/config.py': [
        (r'parents\[1\]', '路径层级，不是文本'),
    ],
    'proto/skill_pack.py': [
        (r'lines\[i \+ 1 :\]|lines\[0\]', 'frontmatter 切分'),
    ],
    'proto/__main__.py': [
        (r'\[seen\[0\] :\]|\[0\]', '增量打印的游标'),
    ],
}


def scan_source() -> None:
    print("① 源码扫描：交接点上的切片，要么豁免、要么不许有")
    unlisted: list[str] = []
    for f in sorted(glob('proto/*.py')):
        key = f.replace(chr(92), '/')
        lines = io.open(f, encoding='utf-8').read().splitlines()
        for n, line in enumerate(lines, 1):
            if line.strip().startswith('#'):
                continue
            if not SLICE.search(line):
                continue
            if any(re.search(rx, line) for rx, _ in ALLOWED.get(key, [])):
                continue
            unlisted.append(f'{key}:{n}  {line.strip()[:100]}')
    check(
        "没有「未申报」的截断",
        not unlisted,
        "以下切片没有豁免理由 —— 交接内容不许悄悄切一刀；\n"
        "确实要切，就在本文件的 ALLOWED 里写明**为什么**（并确保切了要标）：\n  "
        + "\n  ".join(unlisted),
    )
    for key, items in ALLOWED.items():
        src = io.open(key, encoding='utf-8').read()
        for rx, why in items:
            check(f"豁免仍然成立：{key} · {why[:28]}", bool(re.search(rx, src)),
                  f"规则 `{rx}` 已经匹配不到了 —— 代码改了就把这条豁免删掉，别留着烂")


# ═══════════════════════════ ② 逐交接点验完整性 ═══════════════════════════

#: 顶穿任何阈值的超长文本（尾标记用来抓截断）
LONG = ("开头标记：这段交代写得很长。" + "内容。" * 400 + "　结尾标记：截断的话它一定不在。")


def main() -> None:
    scan_source()

    from langchain_core.messages import AIMessage, HumanMessage

    from proto.agent import _ask_prompt, render_briefing, render_receipt
    from proto.artifacts import ARTIFACTS, store_artifact
    from proto.executor import ExecOutcome, _Judgement, _nudge_text
    from proto.graph import node_prepare

    print("\n② 逐个交接点：喂超长输入，断**一个字都没丢**")

    # ── ④ 交接书（平台 → 执行器）──
    ARTIFACTS.clear()
    aid = store_artifact("超长产物", "梗概" * 40, "md", "正文" * 900)
    task = {
        "id": "t1", "skill": "s", "task": "超长任务描述：" + "细节。" * 200,
        "stage": "waiting_for_input", "artifacts": [aid], "refs": [aid],
        "report": LONG, "progress": LONG, "missing": [LONG],
        "qa": [{"by": "用户", "q": LONG, "a": LONG}],
        "user_turns": [{"n": 1, "text": LONG, "after": LONG}],
        "based_on": [1],
    }
    brief = render_briefing(task)
    check("④ 交接书 · 任务全文在", task["task"] in brief)
    check("④ 交接书 · 执行器上次说的全文在", LONG in brief)
    check("④ 交接书 · 判定给的进度全文在", task["progress"] in brief)
    check("④ 交接书 · 缺什么全文在", task["missing"][0] in brief)
    check("④ 交接书 · 用户原话全文在（点名的那轮）", task["user_turns"][0]["text"] in brief)
    check("④ 交接书 · 它在回答的上一句全文在", task["user_turns"][0]["after"] in brief)
    check("④ 交接书 · 问答对全文在", task["qa"][0]["a"] in brief)

    # ── ⑧ 推回去（平台 → 执行器）──
    nudge = _nudge_text(_Judgement(status="incomplete", progress=LONG, missing=[LONG], reason=""))
    check("⑧ 推回去 · 进度全文在", LONG in nudge)
    check("⑧ 推回去 · 缺失项全文在", nudge.count(LONG) >= 2)

    # ── ⑨ 试答（平台 → 主 agent）──
    outcome = ExecOutcome(status="waiting_for_input", artifacts=[], report=LONG, rounds=1,
                          progress=LONG, missing=[LONG])
    ask = _ask_prompt("s", "超长任务：" + "细节。" * 200, outcome,
                      [HumanMessage(LONG), AIMessage(LONG)])
    check("⑨ 试答 · 执行器问的全文在", LONG in ask)
    check("⑨ 试答 · 主 agent 上下文里那条长消息**全文在**", LONG in ask)
    check("⑨ 试答 · 主 agent 上下文里那条长回复**全文在**", ask.count(LONG) >= 2)
    check("⑨ 试答 · 没有「最近 N 条」这种无声范围限制", "最近" not in ask.split("你的上下文")[1][:40])

    # ── ⑩ 回执（执行器 → 主 agent）—— 完整断言在 `unit_receipt.py`，这里只做交叉 ──
    receipt = render_receipt({}, outcome)
    check("⑩ 回执 · 执行器说的全文在（详见 unit_receipt.py）", LONG in receipt)

    # ── ② 台账注入（平台 → 主 agent）—— **每轮重复注入的索引，允许短，但必须标 ──
    ARTIFACTS.clear()
    st = {
        "messages": [AIMessage("（没有 HumanMessage，免得触发投递）")],
        "tasks": [{"id": "t1", "skill": "s", "stage": "waiting_for_input",
                   "artifacts": [], "refs": [], "report": LONG,
                   "progress": LONG, "missing": [LONG], "qa": [], "user_turns": []}],
    }
    injected = node_prepare(st)["messages"][0].content
    check("② 台账 · 长内容被截了（它是每轮重复注入的索引，允许短）", LONG not in injected)
    check("② 台账 · **但截了必须说**：带了平台截断标记", "【平台截断" in injected)
    check("② 台账 · 说明了还差多少字 / 去哪看全文",
          "全文见那次的回执" in injected)
    short_task = {**st["tasks"][0], "report": "很短", "progress": "也很短", "missing": ["短"]}
    check("② 台账 · 短内容**不该**被标截断",
          "【平台截断" not in node_prepare({**st, "tasks": [short_task]})["messages"][0].content)

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
