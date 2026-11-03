"""单元级：**决策记录**（不调 LLM）—— 一次 LLM 决策 = 一条记录，四段齐全。

    uv run python -m evals.unit_decision

用户 2026-11-03：*"decide 那一段日志也得整理下格式，人看着也不清晰。既然是决策，
很显然：**决策前提是什么、要决策的问题是什么、决策结果是什么**，有可能还要补充
**下一步该干什么** —— 你现在的打印也是人类不好看懂。"*

⚠️ **这个测试管不了"模型读懂没有"** —— 那要真跑（`uv run python -m evals.chat`）。
它管的是**机制**，就两件事：

**① 记录层**：`trace.decision` 渲出来的四段**齐全、顺序对、值一个字不丢**，
   而且列表**不是 Python repr**（原来是 `['…', '…']`，括号引号全是噪音）。

**② 记录层 ↔ 展示层不许失配**：`evals/chat.py` 认决策块**只能从 `trace.DECISION_HEAD` 取**。
   写死内容特征（`"轮判定：status="` 那种）的后果是**静默**的：措辞一改，
   块认不出来 → 整块当噪音丢掉 —— 而丢的正是"平台为什么这么决定"。
   同 `handoff.SECTION` 那条教训：harness 手抄段名，回执改了格式之后
   **新格式 0/8 命中**，12560 字的回执被静默切成 1200。

> 有效性自检：把 `chat.py` 里那句 `body.startswith(trace.DECISION_HEAD)` 换回
> 写死的字符串，② 必须红。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import io
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from proto import trace

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail and not ok else ""))


def _capture(fn) -> str:
    """跑一段记录，把它**新写进日志的那几行**捞出来（不碰别的输出）。"""
    before = trace.LOG_FILE.stat().st_size
    fn()
    with io.open(trace.LOG_FILE, "rb") as f:
        f.seek(before)
        return f.read().decode("utf-8", "replace")


#: 一段**真实形状**的判定结果（含多行值、含列表）—— 空值测不出"漏渲染"。
def _sample() -> None:
    trace.decision(
        "判定（LLM）· R02 第 1 轮",
        q="这个任务完成了没？",
        given=[
            "技能 lesson-prep（instructions 2532 字，按名引用）",
            "材料 执行器散文（无产物）",
        ],
        result=[
            "status = **waiting_for_input**",
            "理由：任务要求生成完整教案，但第二个问题还没收到答复。",
            "还缺：\n- 尚未收到用户对第二个问题的答复\n- 教案文本文件产物为空",
        ],
        nxt="登记待答 → 平台强制主 agent 试答 → 答不上就**原样透传**给用户",
    )


def main() -> None:
    got = _capture(_sample)
    print("① 四段齐全、顺序 = 用户定的那个顺序（前提 → 问题 → 结果 → 下一步）")
    heads = [f"{trace.DECISION_HEAD} · ", *[f"    {v}：" for v in trace.DECISION.values()]]
    for h in heads:
        check(f"`{h.strip()}` 在", h in got, repr(got[:120]))
    pos = [got.find(f"    {v}：") for v in trace.DECISION.values()]
    check("四段的**先后顺序**对", pos == sorted(pos) and all(p >= 0 for p in pos), f"位置={pos}")
    check("表里的段名**都用到了**（没有死段）",
          all(f"{v}：" in got for v in trace.DECISION.values()))

    print("\n② 值**一个字都不丢**，且列表**不是 Python repr**")
    for frag in ("技能 lesson-prep（instructions 2532 字，按名引用）",
                 "status = **waiting_for_input**",
                 "理由：任务要求生成完整教案，但第二个问题还没收到答复。",
                 "尚未收到用户对第二个问题的答复",
                 "教案文本文件产物为空",
                 "登记待答 → 平台强制主 agent 试答 → 答不上就**原样透传**给用户"):
        check(f"`{frag[:22]}…` 原样在", frag in got)
    check("列表**逐条成行**（`- ` 起头），不是 `['…', '…']` 那种 repr",
          "      - 尚未收到用户对第二个问题的答复" in got and "['" not in got)

    print("\n③ 展示层认块**从 `DECISION_HEAD` 取** —— 不许写死内容特征")
    src = io.open("evals/chat.py", encoding="utf-8").read()
    check("`chat.py` 用了 `trace.DECISION_HEAD`", "trace.DECISION_HEAD" in src)
    # 旧的两条写死特征：措辞一改就静默失配（改了措辞，展示层还去找老字符串）。
    # ⚠️ **只看代码，跳过整行注释** —— 注释里为了说明"原来错在哪"会引用旧写法，
    #    那不是代码（同 `unit_handoff.scan_source` 的做法）。
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    for lit in ("轮判定：status=", "判定输入："):
        check(f"`chat.py` 的**代码**里没有写死的 `{lit}`", lit not in code)

    print("\n④ 两个决策点都走 `trace.decision`（不再是散装 note）")
    for path in ("proto/executor.py", "proto/agent.py"):
        s = io.open(path, encoding="utf-8").read()
        check(f"`{path}` 调了 `trace.decision(`", "trace.decision(" in s)
    check("`executor.py` 不再自己拼「判定输入」那条 note", 'f"判定输入：' not in
          io.open("proto/executor.py", encoding="utf-8").read())

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
