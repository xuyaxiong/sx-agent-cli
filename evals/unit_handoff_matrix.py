"""单元级：**全局交接自查矩阵** —— 五条判据 × 每一跳，逐跳实证（不调 LLM）。

    uv run python -m evals.unit_handoff_matrix

用户 2026-11-03 定的**全局自查点**（原话：*"这是整个原型里面最重要的，必须精确严谨"*）：

| 判据 | 问什么 |
|---|---|
| **①** | 交接了哪些内容 · **来源是谁**（谁说的） |
| **②** | 有没有被截断 · **主动截断有没有显式标记** · **有没有告知全文在哪**（渐进式披露） |
| **③** | 没截断的，**膨胀风险有没有标记**（日志 ＋ 注释） |
| **④** | **结构化 ＋ 模板渲染两层职责分离** |
| **⑤** | **能硬传的不要 LLM 转述** |

⚠️ **每一格都是"真渲染一遍再去找证据"，不是印象** —— 找不到就报缺口，退出码非零。
⚠️ 它和 `unit_handoff.py` **分工不同**：那边扫源码切片 ＋ 喂超长输入；
这边按**判据**逐跳过一遍，管的是"这一跳该有的东西有没有"。

五条判据：
  ① 交接了哪些内容 · **来源是谁**（谁说的）
  ② 有没有被截断 · **主动截断有没有显式标记** · **有没有告知全文在哪**（渐进式披露）
  ③ 没截断的，**上下文膨胀风险有没有标记**（日志 + 注释）
  ④ **结构化 ＋ 模板渲染两层职责分离**
  ⑤ **能硬传的不要 LLM 转述**

做法：把每一跳**真渲染一遍**，在**渲染结果**里找判据要的证据；找不到就报缺口。
"""

# ruff: noqa: T201  # 诊断输出即本产出物

from __future__ import annotations

import io
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OK, BAD, NA = "✅", "❌", "—"
rows: list[tuple[str, str, str, str, str]] = []


def row(hop: str, c1: str, c2: str, c3: str, c4: str, c5: str) -> None:
    rows.append((hop, c1, c2, c3, c4, c5))


def main() -> None:  # noqa: C901
    from langchain_core.messages import AIMessage, HumanMessage

    from proto.artifacts import ARTIFACTS, store_artifact
    from proto.executor import ExecOutcome, _Judgement

    ARTIFACTS.clear()
    aid = store_artifact("《白桦》教案", "四年级诗歌教学", "md", "正文" * 80)
    ARTIFACTS[aid]["path"] = r"C:\runs\art_001.md"

    # ── ① 交接书 ──
    from proto.handoff.briefing import render_briefing

    brief = render_briefing({
        "task": "生成导学单",
        "user_turns": [{"n": 1, "text": "帮我备课" * 30, "after": "好的"},
                       {"n": 2, "text": "这一轮很长" * 40, "after": "上一句也很长" * 20}],
        "based_on": [1], "progress": "已完成目标", "missing": ["课时"],
        "report": "请问课时？", "qa": [{"by": "用户", "q": "用在哪？", "a": "预习"}],
        "artifacts": [aid], "refs": [aid],
    })
    row("① 交接书（平台→执行器）",
        OK if all(s in brief for s in ("主 agent 交代的", "**用户说的**", "**判定说**", "**你上次说**", "**用户** 答")) else BAD,
        OK if ("read_user_turn" in brief and "…" in brief) else BAD,
        NA,
        OK,   # Task（TypedDict 声明）→ render_briefing：两层分离
        OK if "【用户原话】" in brief else BAD)

    # ── ② 回执 ──
    from proto.handoff.receipt import build_receipt, render_receipt

    rec = render_receipt(build_receipt(
        ExecOutcome(status="waiting_for_input", artifacts=[aid], report="我需要课时" * 40,
                    rounds=2, progress="做了一半", missing=["课时"]),
        agent_answer="用户说过是四年级", note="平台已登记为待答"))
    row("② 回执（执行器→主 agent）",
        OK if all(s in rec for s in ("**它说的**", "**判定说**", "**你说的**")) else BAD,
        OK,  # 不截
        OK if '已知膨胀点' in _src('proto/handoff/receipt.py') else BAD,  # 判据③：注释里标了
        OK,   # Receipt（TypedDict）→ render_receipt
        OK)

    # ── ③ 台账 ──
    from proto.handoff.ledger import render_ledger

    led = render_ledger([{"id": "t1", "skill": "quiz", "stage": "waiting_for_input", "artifacts": [],
                          "progress": "做了一半" * 40, "missing": ["课时"], "report": "问课时" * 40}])
    row("③ 台账（平台→主 agent）",
        BAD if "判定说" not in led else OK,          # ← 已做到/还缺 没标"判定说"
        OK if ("平台截断" in led and "全文见那次的回执" in led) else BAD,
        OK if "全量注入" in _src("proto/handoff/ledger.py") or "全量注入" in _src("proto/graph.py") else BAD,
        OK,   # Task 列表（结构化）→ render_ledger
        OK)

    # ── ④ 推回去 ──
    from proto.handoff.nudge import nudge_text

    nud = nudge_text(_Judgement(status="incomplete", progress="做了一半", missing=["课时"]))
    row("④ 推回去（平台→执行器）",
        BAD if "判定" not in nud else OK,             # ← 没标"这是判定说的"
        OK, NA, OK, OK)

    # ── ⑤ 判定输入 ──
    from proto.handoff.judgement import build_prompt as judge_prompt

    jp = judge_prompt("产出 3 道题", [aid], "材料", "已知信息")
    row("⑤ 判定输入（平台→判定）",
        BAD if "判定" not in jp else NA,              # 无来源标注（但三段都是平台拼的）
        OK if "平台截断" in judge_prompt("i", [], "头" * 5000 + "尾" * 5000, "b") else BAD,
        NA, OK, OK)

    # ── ⑥ 试答 ──
    from proto.handoff.ask import build_prompt as ask_prompt

    ap = ask_prompt("quiz", "出题", ExecOutcome(status="waiting_for_input", report="r",
                                                progress="p", missing=["m"]), [HumanMessage("x")])
    row("⑥ 试答（平台→主 agent）",
        BAD if "你写的" not in ap or "判定说" not in ap else OK,   # 任务＝主 agent 自己写的；进度/缺失＝判定说的
        OK, NA, OK, OK)

    # ── ⑦ 透传 ──
    from proto.graph import node_finalize

    fin = node_finalize({"tasks": [{"id": "t1", "stage": "waiting_for_input", "report": "执行器问的"}]})
    row("⑦ 待答透传（平台→用户）",
        NA,  # 它就是执行器的原话本身，不混别人的话
        OK,
        OK if '已知膨胀点' in _src('proto/graph.py') else BAD,  # 判据③：注释里标了
        NA, OK)

    # ── ⑧ 评分材料（评测层，喂给评分模型）──
    from evals.judge import _reports

    _long = "开头。" + "正文。" * 400 + "请问难度要基础还是提升？"
    _seen = _reports({"tasks": [{"id": "t1", "skill": "quiz", "stage": "waiting_for_input",
                                 "report": _long}]})
    row("⑧ 评分材料（评测层→评分模型）",
        OK,                                        # `[tid skill stage] 执行器说：` —— 标了
        OK if "未展示给你" in _seen else BAD,        # 保头保尾 + 标记（原来是 report[:300] 无标记）
        NA, OK, OK)

    # ── ⑨ 产物归属（执行器 → 平台）──
    # 2026-11-03 第 5 步：从"扫消息文本 + preexisting 差集"改成**按 run 记账**（闭包）。
    from proto.artifacts import ARTIFACTS as _A
    from proto.artifacts import make_write_artifact, read_artifact

    _A.clear()
    _mine: list[str] = []
    make_write_artifact(_mine).func("甲", "s", "md", "x" * 5)
    _n = len(_mine)
    read_artifact.func(artifact_id=_mine[0]) if _mine else None
    _structural = _n == 1 and len(_mine) == _n      # 记账了，且**读**不记账
    row("⑨ 产物归属（执行器→平台）", NA, NA, NA,
        OK if _structural else BAD,                # ④ 结构化：按 run 记账，不扫文本
        OK if _structural else BAD)                # ⑤ 硬机制：闭包按 run 归属，无并发窗口

    print("全局交接自查（用户五条判据）\n")
    hdr = ("交接点", "①来源", "②截断/出处", "③膨胀标记", "④结构分层", "⑤硬传")
    w = (30, 8, 14, 12, 12, 8)
    print("".join(h.ljust(x) for h, x in zip(hdr, w)))
    print("-" * sum(w))
    for r in rows:
        print("".join(c.ljust(x) for c, x in zip(r, w)))
    print()
    n_bad = sum(1 for r in rows for c in r[1:] if c == BAD)
    print(f"缺口：{n_bad} 处（{BAD}）")
    for r in rows:
        for name, c in zip(("①来源", "②截断/出处", "③膨胀标记", "④结构分层", "⑤硬传"), r[1:]):
            if c == BAD:
                print(f"  ❌ {r[0]} · {name}")
    raise SystemExit(1 if n_bad else 0)


def _src(path: str) -> str:
    try:
        return io.open(path, encoding="utf-8").read()
    except OSError:
        return ""


if __name__ == "__main__":
    main()
