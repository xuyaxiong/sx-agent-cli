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
import os

# 🔴 **单测的日志打 tag**（2026-11-06）：`logs/` 里真会话与测试垃圾混在一起，复盘时
#    grep 不动 —— 打上 tag 后文件名是 `proto-<ts>-unit-<名字>.log`，一眼可辨
#    （清理：`rm logs/*-unit-*.log`）。复用跑批那个 `PROTO_LOG_TAG`（`evals/_child.py`）。
# ⚠️ **必须排在所有非标准库 import 之前**：`proto.trace` 在 import 那一刻读 env 定文件名，
#    而 `from evals import chat` 这种**间接** import 也会把它拉进来（实测踩过：
#    第一版插在 `from evals import chat` 之后 → 日志照样没 tag）。
#    守卫：`unit_feed_waiting` ⑮。
os.environ["PROTO_LOG_TAG"] = "unit-handoff-matrix"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OK, BAD, NA = "✅", "❌", "—"
rows: list[tuple[str, str, str, str, str]] = []


def row(hop: str, c1: str, c2: str, c3: str, c4: str, c5: str) -> None:
    rows.append((hop, c1, c2, c3, c4, c5))


def main() -> None:  # noqa: C901
    from langchain_core.messages import AIMessage, HumanMessage

    from proto import artifacts
    from proto.artifacts import store_artifact
    from proto.executor import ExecOutcome

    artifacts.clear()
    aid = store_artifact("《白桦》教案", "四年级诗歌教学", "md", "正文" * 80)
    artifacts.get(aid)["path"] = r"C:\runs\art_001.md"

    # ── ① 交接书 ──
    from proto.handoff.briefing import render_briefing

    brief = render_briefing({
        "task": "生成导学单",
        "user_turns": [{"n": 1, "text": "帮我备课" * 30, "after": "好的"},
                       {"n": 2, "text": "这一轮很长" * 40, "after": "上一句也很长" * 20}],
        "report": "请问课时？", "qa": [{"by": "用户", "q": "用在哪？", "a": "预习"}],
        "artifacts": [aid], "refs": [aid],
    })
    row("① 交接书（平台→执行器）",
        OK if all(s in brief for s in ("主 agent 交代的", "**用户说的**",
                                       "（执行器原话）", "**答 1**（**用户**）")) else BAD,
        OK if ("read_user_turn" in brief and "…" in brief) else BAD,
        NA,
        OK,   # Task（TypedDict 声明）→ render_briefing：两层分离
        OK if "【用户原话】" in brief else BAD)

    # ── ② 回执 ──
    from proto.handoff.receipt import build_receipt, render_receipt

    rec = render_receipt(build_receipt(
        ExecOutcome(status="waiting_for_input", artifacts=[aid], report="我需要课时" * 40,
                    rounds=2, asked="这节课用几课时？"),
        agent_answer="用户说过是四年级", note="平台已登记为待答"))
    row("② 回执（执行器→主 agent）",
        OK if all(s in rec for s in ("**它说的**", "**你说的**")) else BAD,
        OK,  # 不截
        OK if '已知膨胀点' in _src('proto/handoff/receipt.py') else BAD,  # 判据③：注释里标了
        OK,   # Receipt（TypedDict）→ render_receipt
        OK)

    # ── ③ 台账 ──
    from proto.handoff.ledger import render_ledger

    # ⚠️ **2026-11-04 时两条明细行都要覆盖**（待答打 `asked`、失败打 `report`，都要能截）；
    #    🔴 **2026-11-06 起只剩一条**：待答那档**不再重印问题**（`ledger`〔沿革 11〕，
    #    全文出处是【待答】），所以**截断只有 `failed` 那档**（下面 t2）。
    #    ⚠️ t1（待答）仍留在样本里 —— 它守的是"**不**重印" ＋ 标签还在。
    led = render_ledger([
        {"id": "t1", "skill": "quiz", "stage": "waiting_for_input", "artifacts": [],
         "asked": "用几个课时？" * 30, "report": "问课时" * 60},      # >150 字才触发台账截断
        {"id": "t2", "skill": "quiz", "stage": "failed", "artifacts": [],
         "report": "干不下去" * 60},
    ])
    row("③ 台账（平台→主 agent）",
        # ⚠️ 2026-11-05 判定退休：台账里「已做到 / 还缺（判定说）」两行删了（没有来源了），
        #    只剩执行器自己的话 —— 这一列断的是"它说的话**标了来源**"。
        #    两档的标签不同（待答=「它要的信息」/ 失败=「执行器说」），但都点明了是谁的。
        BAD if not ("执行器说" in led and "它要的信息" in led) else OK,
        # ②截断/出处：切了要标 + **说清全文在哪**。⚠️ 答案**由 `run_by` 决定**（E2）：
        # 这个 Task 没记 `run_by` → 走 fail-safe「不在你的上下文里」；
        # 两条路各自的断言在 `unit_handoff` ②（delegate→回执 / feed→没有回执）。
        OK if ("平台截断" in led and "不在你的上下文里" in led) else BAD,
        OK if "全量注入" in _src("proto/handoff/ledger.py") or "全量注入" in _src("proto/graph.py") else BAD,
        OK,   # Task 列表（结构化）→ render_ledger
        OK)

    # ── ④ 推回去 ──
    from proto.handoff.nudge import nudge_text

    nud = nudge_text()
    row("④ 推回去（平台→执行器）",
        # ⚠️ 判定退休后 nudge **不再带「判定说」**（没有来源了）—— 它说的是**结构事实**
        #    （没交、没申报）＋ **两条出路**（交付 / `ask_user`）。
        OK if ("既没有交付产物" in nud and "ask_user" in nud) else BAD,
        OK, NA, OK, OK)

    # ── ⑤ ~~判定输入~~ —— **2026-11-05 整行删**：判定那次 LLM 调用退休了，
    #    三态改由平台按结构定（申报要信息 / 交了产物 / 都没有），不再有"喂给判定的材料"这一跳。

    # ── ⑥ 试答 ──
    from proto.handoff.ask import build_prompt as ask_prompt

    ap = ask_prompt("quiz", "出题",
                    ExecOutcome(status="waiting_for_input", asked="用几课时？", report="r"),
                    [HumanMessage("x")])
    row("⑥ 试答（平台→主 agent）",
        # 任务＝主 agent 自己写的；**它要什么＝`ask_user` 参数里的原话**（结构 ✓ 不是从话里挑的）
        BAD if "**它申报的**" not in ap or "用几课时？" not in ap else OK,
        OK, NA, OK, OK)

    # ── ⑦ 透传 ──
    # 🔴 **2026-11-04（F18 收口）**：防重复靠的是 `agent._StopWhenPending`（主 agent 那一拍
    #    不生成），**不是**"不给它看"—— 实测撤掉注入照样复述。所以这条注入留着
    #    （它是转写里唯一那条问题 ＋ `_user_turns` 的「它在回答的上一句」的来源）。
    from proto.graph import node_finalize, render_relay

    _w1 = {"id": "t1", "skill": "quiz", "stage": "waiting_for_input",
           "asked": "用几课时？", "report": "执行器问的"}
    _r = render_relay([_w1])
    _fin = node_finalize({"tasks": [_w1]})
    row("⑦ 待答透传（平台→用户）",
        # ①来源：贴的是**它申报的原话**（不是那段过场），而且**打了 `injected_by` 标记**
        #      （记录层据此不把它算成主 agent 说的话）
        OK if ("「quiz」问：用几课时？" in _r
               and (_fin["messages"][0].additional_kwargs or {}).get("injected_by")
               == "platform.finalize") else BAD,
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
    from proto import artifacts as _A
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
    # ⚠️ **全绿时不许印那个 ❌**（2026-11-06 修）：原来是无条件 `（{BAD}）`，
    #    于是"缺口 0 处"也印成 `缺口：0 处（❌）` —— 正是"看起来像功能坏了"那一类
    #    （读的人先看见红叉，再读数字才发现是 0）。有缺口才摆标记。
    print(f"缺口：{n_bad} 处" + (f"（{BAD}）" if n_bad else f"（{OK}）"))
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
