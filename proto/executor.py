"""Sub Agent = 可选执行者（按需创建、通用、隔离上下文）。

三条约束：
1. **不绑 skill** —— 技能指令由主 agent 随任务传进来。加一个技能不需要动平台。
2. **用完即弃** —— 不跨轮存活、不挂起、不与用户交互。
   （状态在 `Task` 上，不在执行器上——所以"重新起一个"是无害的。）
3. **不许随便停** —— 它必须以三态之一结束：
   `completed`（任务完成）/ `waiting_for_input`（在要信息）/ `incomplete`（都没）。
   **`incomplete` → 平台把它推回去继续 react**（最多 `MAX_REACT_ROUNDS` 轮），
   而不是让用户承担它没干完的后果。

**判定怎么问，很关键**：问「**任务完成了没**」，不是「产物生成了没」。
产物只是**证据之一**——纯分析任务可能没有文件产物，半成品可能有文件。
（这是评审人 C2 的落点，也是用户明确纠正过的问法。）

**判定所需的信息一样都不能少**（`instructions` + `briefing` + 产出 + 产物数量）——
**判定看不到它需要的信息，就只能猜**（这个坑踩过两次）。
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field

from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage

from proto import trace
from proto.config import get_model
from proto.handoff import SECTION, OutcomeStatus as _OutcomeStatus, render_hop
#                ↑ 状态词汇（唯一一份）· 段名/渲染（唯一一份）
# ⚠️ 判定那套（`_Judgement` / 规则 / 材料 / 截断 / `build_prompt`）**整块退休**（2026-11-05）：
#    "任务结没结束"不再由一次 LLM 调用判 —— 现在是三件**平台看得见的**结构事实
#    （申报要信息 / 交了产物 / 都没有，见 `run_task` 末尾）；"够不够"由主 agent 判。
from proto.handoff.nudge import nudge_text  # 推回去的模板
from proto.artifacts import (
    read_artifact,
    make_write_artifact,
)
from proto.skill_pack import read_skill_file, skill_file_index_lines  # 技能包文件（磁盘）
from proto.skills import Skill  # 技能领域

#: 平台约定 —— 尽量少。技能作者写什么都不会破坏它，因为它不要求作者做任何事。
_PLATFORM_RULE = (
    "你是任务执行者。按下面的技能指令完成任务。\n"
    "要交付成文件的内容，调用 write_artifact 写入产物库：**名称、一句话梗概、正文**三样都给，\n"
    "正文写全（梗概是对外露出的那一层，别人靠它决定要不要读全文）。\n"
    "任务里列出的【可读产物】只是一份**清单**（名称与梗概）——需要全文时用 read_artifact 读，\n"
    "不要凭标题或梗概猜内容。\n"
    "\n"
    "如果缺少完成任务所必需的信息：\n"
    "- **明确说明你需要什么**（缺哪个参数、要什么确认）——你的请求会被转给能提供它的一方；\n"
    "- **不要编造**，也不要用「已完成」之类的话收尾。\n"
    "\n"
    "⚠️ **【用户答复】里不一定真的回答了你的问题**（用户可能换了个话题，或答非所问）。\n"
    "拿到答复后先核对：**你缺的那项信息，它给了吗？**\n"
    "- 给了 → 正常干活；\n"
    "- **没给 → 再问一次，不要自己挑一个默认值硬做。**\n"
    "  你猜的那个值会变成一份**基于错误前提的产物**，而用户不知道 —— 那比多问一句糟得多。\n"
    "  也不要顺手把答复里提到的**别的**需求做掉：那不是你的任务。\n"
)

#: 一个任务最多让它 react 几轮（平台自保，不约束 skill 作者）
MAX_REACT_ROUNDS = 3

#: 每次 `run_task` 一个递增编号 —— 日志里用来**区分不同的 run**
#: （一次进程会跑很多次 run_task，没有编号就分不清哪一行属于哪一次）
_RUN_SEQ = itertools.count(1)


#: 出口三态 —— **词汇表在 `proto/handoff`**（2026-11-04 复审 N4：原来这里和
#: `_Judgement.status` 各写一遍同一串字面量，改一处就静默分叉）。
#: 这里再导出一次只是为了不打断既有引用（`ExecOutcome.status` 的标注用它）。
OutcomeStatus = _OutcomeStatus


@dataclass
class ExecOutcome:
    """执行器的结果 —— 平台据此决定任务状态。

    ⚠️ **状态用枚举，不用两个布尔值**（用户原则 ②：决策结果要结构化）。
    两个布尔值有 4 种组合、只有 3 种合法，"矛盾组合"只能靠约定排除；
    枚举则**从类型上就闭合**。

    ⚠️ 2026-11-05：原来还有 `progress` / `missing`（**判定**吐的"已做到 / 还缺"）——
    判定退休后没有来源了，两个字段整条链一起摘掉（`Task` / 交接书 / 台账 / 回执）。
    """

    #: 三态之一 —— 与 `Task.stage` **前三个同名**（`completed` / `waiting_for_input`），
    #: 唯一映射是 `incomplete → failed`。**没有第四种**。
    status: OutcomeStatus = "incomplete"
    artifacts: list[str] = field(default_factory=list)
    report: str = ""
    #: 实际 react 了几轮
    rounds: int = 0
    #: 🔴 **它申报要的信息**（`ask_user` 工具里那句原话；空 = 没申报）。
    #:
    #: 2026-11-05：在这之前，"我要信息"只能**写在回复正文里**，靠判定读文本猜 ✗。
    #: 现在它是**结构**：执行器调了 `ask_user` → 平台直接看见 ✓ → 走"试答 → 转用户"。
    asked: str = ""
    #: 🔴 **平台故障**（非空 = 这一轮**根本没跑成**，不是执行器干不了）。
    #:
    #: 2026-11-04 复审 N2/R8：`stream_agent` 抖两次之后抛到这里，`run_task` 把它接住、
    #: 记成这个字段，而不是让异常穿出去炸掉整轮（用户白等 3 分钟 + 一屏栈）。
    #: ⚠️ **不能塞进 `report`**：回执里【执行器说】的来源标注是"**它说的**"，
    #: 而这句话是**平台说的** —— 塞进去正好污染"来源标注不能串"这条规矩。
    #: ⚠️ 与 `status` 的关系：故障时 `status` 仍是 `incomplete`（三态是封闭的，没有第四种），
    #: **主 agent 该怎么做**由这个字段决定 —— 见回执的【平台故障】段。
    fault: str = ""


def make_ask_user(asked: dict):
    """造 `ask_user` 工具 —— 执行器**要信息**的唯一出口（**结构**，不是正文里的一句话）。

    🔴 2026-11-05（判定退休）：在这之前，"我要信息"只能**写在回复正文里**，
    平台得**读文本**才知道（那是判定的活 ✗）—— 于是"要不要把执行器说的话喂给判定"
    成了死结（喂了：礼貌收尾被当成在要信息 ✗；不喂：真问题被吞 ✗）。

    现在它**调工具**，平台**直接看见**两件事：
      - **它卡住了**（要信息）✓ 不用猜
      - **它要什么**（`question` 里那句**原话**）✓ 不用从正文里挑哪句是问题

    之后：平台先让主 agent 试答（它上下文里可能就有）→ 答不上把 `question` **原样**转用户。

    ⚠️ **这一轮到此结束**：`run_task` 看到调用就 `break`（不等它继续说 ✓ 结构 ✓）——
    它调完工具还会接着往下说，那半截话不进这一轮的结论。
    ⚠️ 和 `make_write_artifact` 一样用**闭包**（按 run 归属），不用模块级全局。
    """

    @tool
    def ask_user(question: str) -> str:
        """你**没法往下干**了，需要一条你自己拿不到的信息 —— 用这个问。

        Args:
            question: **具体、能直接回答**的一句话（如"这节课用几课时？"）。
                一次问一件；**能自己判断的别问**（会打扰用户）。

        ⚠️ **要信息必须用它**：写在回复正文里**平台看不到**（平台不读正文）。
        """
        asked["question"] = str(question or "").strip()
        return "（已登记：平台会先问主 agent，它答不上再转用户）"

    return ask_user


def _user_turn_reader(user_turns: list[dict]):
    """造一个**只认这次交接里那几轮**的读取工具（渐进式披露第三层）。

    ⚠️ **用闭包，不用模块级全局**：并行 delegate 时两个执行器会互相覆盖 ——
    `ARTIFACT_DIR` 那种全局状态在这儿就是坑（同源教训见 `graph._merge_tasks`）。
    """
    by_n = {int(t["n"]): t for t in user_turns}

    @tool
    def read_user_turn(turn: int) -> str:
        """读某一轮**用户原话的完整上下文**：原话全文 ＋ **它在回答的那一句**。

        交接书【用户原话】那一段列了每一轮的开头；**确定这一轮确实用得上再读** ——
        别把每一轮都拉进上下文。

        ⚠️ 像「A」这种**短应答**，脱离它回答的问题就没有意义（选项列表在那一句里）——
        所以这里给的是**整轮**，不只是那几十个字。
        """
        if turn not in by_n:
            return f"没有第 {turn} 轮。现有：{'、'.join(str(n) for n in sorted(by_n))}"
        t = by_n[turn]
        # ⚠️ **不用 `【…】`**：它是**工具返回的头**，不是交接单的段名 ——
        #    `【…】` 独占行首在这套系统里等于"这是个段名"（记录层靠它切段）。
        out = [f"**用户原话 · 第 {turn} 轮**", str(t.get("text") or "")]
        after = str(t.get("after") or "").strip()
        if after:
            out += [
                "",
                "—— **它在回答的上一句**（可能是主 agent 说的，也可能是平台把执行器"
                "自己的问题透传给用户的那条）：",
                after,
            ]
        return "\n".join(out)

    return read_user_turn


def run_task(skill: Skill, briefing: str = "", user_turns: list[dict] | None = None) -> ExecOutcome:
    """通用执行器：干活、**判定出口**、没出口就推它继续。

    Args:
        skill: 专业技能包（指令从这里来）。
        briefing: 这次交给它的全部已知 —— 由 `agent.render_briefing(task)` 渲染。
            平台**不解析、不抽取、不比对**，原样带给执行器。
            ⚠️ 名字是 briefing 不是 given：它是**整份交接**（任务 / 上次做到哪 / 历次答复 /
            已产出产物 / 可读产物），不是"这一轮的增量"—— 执行器用完即弃，每次都是重入。
            ⚠️ **可读产物清单也由它承载**（原来是 `run_task` 单独拼一个 `artifacts` 参数，
            两处都渲染 → 执行器看到两遍；现在只有一个来源）。
        user_turns: 用户原话（`[{"n": 1, "text": "..."}]`）—— 只给 `read_user_turn` 工具用。
            **交接书里只渲染清单**（每轮一行、开头几十字），全文按需取：
            用户可能粘长文，一上来全加载就把执行器的上下文塞满了（渐进式披露）。

    Returns:
        `ExecOutcome` —— 平台据此定任务状态（**不再靠"产物库空不空"猜**）。
    """
    #: 🔴 **这次 run 写了哪些产物 —— 由工具自己记**（闭包，按 run 归属）。
    #: 原来是**扫消息文本**猜（`_recover_artifacts` + `preexisting` 差集）——
    #: 既不是结构化、也不是硬机制，而且并发时差集还会互相覆盖。见 `make_write_artifact`。
    #:
    #: ⚠️ **清单里没有"哪份是成品"这个概念 —— 这是定的，别再往这里加草稿/成品的区分**
    #: （`docs/solution.md` §五 **X5**）：一轮里写两份就交两份。要平台标"最终版"＝要它
    #: **读内容判断**"这次是重写还是又交了一样东西" —— 那是**质量判断**，判定就是死在
    #: 这上面。真要做只能让**模型自己声明**（新机制、且靠自觉），没被咬到之前不造。
    created: list[str] = []
    #: `ask_user` 申报的内容（闭包按 run 归属，同 `created`）
    asked: dict = {}
    tools = [
        make_write_artifact(created),
        read_artifact,
        read_skill_file,
        make_ask_user(asked),
    ]
    if user_turns:
        tools.append(_user_turn_reader(user_turns))
    agent = create_agent(
        get_model(),
        tools=tools,
        system_prompt=_PLATFORM_RULE,
        name="executor",
    )

    briefing_text = briefing.strip() or "（暂无 —— 你需要的任何信息都要先问，不要猜）"
    # 技能自带文件时，把**清单**（路径 + 字数）一起给执行器 —— 正文按需 `read_skill_file` 读。
    # 清单**现场扫盘**（不缓存在 `Skill` 上，见 `skill_pack` 的模块 docstring）；
    # **没有自带文件就一个字不加**（单文件技能本来就没有）。
    refs = ""
    have = skill_file_index_lines(skill.name)
    if have:
        # 🔴 **必须把 `skill_name` 写进去**（2026-11-02 实测踩到）：清单里只有路径，
        # 而 `read_skill_file(skill_name, path)` 的**第一个参数是技能名** ——
        # 不写，执行器就只能**猜**：实测它试了「课堂活动配置生成」「classroom-activity-config」
        # 「activity-config」「config」四个变体，全返回"没有自带文件"，
        # 卡了 121 秒后放弃，反过来问用户"这个技能的确切名称是什么"。
        # ⚠️ 这个坑只在"**技能包有 references 且活是委派出去的**"时才出现 ——
        # 走委派的现有技能都没有 references，有 references 的真技能又被主 agent 自己干了，
        # 所以一直没暴露；`EXEC_PATH=delegate` 一开，第一次跑就撞上。
        # ⚠️ **不用 `【…】`**（那是**段名的专用符号**，独占行首就等于声明"这是个段名"，
        #    而记录层靠这两条做确定性切分）—— 它是【技能指令】**里面**的一段，
        #    不是和它平级的段。
        refs = (
            f"\n\n**技能自带文件** —— 要用哪一份就用 `read_skill_file` 读全文："
            f"**`skill_name` 传「{skill.name}」**，`path` 照抄下面的清单"
            "（**不要凭文件名猜内容**）：\n" + "\n".join(have)
        )
    # 🔴 **走唯一那条渲染路**（2026-11-05）：原来两个头都是就地写的字面量，
    #    而且**嵌套是平的**（【已知信息】和它里面的【任务】【用户原话】同在 0 列）。
    #    现在交接书整块缩在【已知信息】下面 —— 哪几段属于它，一眼看得出。
    task_text = render_hop("task_text", {
        "skill_instructions": f"{skill.instructions}{refs}",
        "known": briefing_text,
    })

    run_id = f"R{next(_RUN_SEQ):02d}"
    trace.section(f"run_task · {run_id} · skill={skill.name}（通用执行器，隔离上下文）",
                  kind="run_begin")
    # 🔴 **技能指令按名引用，不逐字复述**（用户要求：读了 skill 只写"读了啥、谁读的"）——
    # 技能原文是静态资产、另有落盘文件；日志里真正要留的是**这次的交接内容**。
    # 交接关键日志：这一跳交了多少、有没有结构化对象（正文在下面那个 block 里）
    trace.handoff("交接书", "平台", "执行器", briefing_text, structured="Task → render_briefing")
    trace.block(
        "发给执行器的内容（技能指令按名引用）",
        f"{SECTION['skill_instructions']}{skill.name}"
        f"（instructions {len(skill.instructions)} 字，原文见该技能包的 SKILL.md）\n\n"
        f"{SECTION['known']}\n{briefing_text}",
        kind="executor_briefing",
    )
    t0 = time.time()
    messages: list = [HumanMessage(task_text)]
    outcome = ExecOutcome()
    for round_i in range(MAX_REACT_ROUNDS):
        # 不传 config —— 传了执行器会变成独立根图。
        # `stream_agent` 边跑边录（每个字都写进日志）—— 一次 LLM 调用那几十秒不再黑屏。
        # ⚠️ `who` 带上技能名：多个执行器**并行**时，流式行会交错，得能分辨是谁。
        # `role` 是**逻辑**（展示层按它归版式），`who` 只是给人看的名字 —— 两者别混。
        resp = {}
        try:
            for chunk in trace.stream_agent(agent, {"messages": messages}, None,
                                            f"执行器·{skill.name}", role=trace.EXECUTOR):
                resp = chunk
        except Exception as e:  # noqa: BLE001
            # 🔴 **基础设施抖动不该炸整轮**（2026-11-04 复审 N2/R8；兑现 `docs/solution.md`
            #    §0.6 的 **N2** 承诺 —— 它原来只在 `safe_structured` 那条路上成立）。
            # `stream_agent` 内部已经重试过一次，**两次都失败才走到这** —— 那就认了：
            # 这一轮记成"**平台故障**"（不是执行器干不了），把决定权交回主 agent。
            # ⚠️ 不在这里重跑：`run_task` 一轮就是 60–120 秒，再重跑是拿用户的时间赌。
            outcome.fault = f"{type(e).__name__}: {e}"
            outcome.rounds = round_i + 1
            trace.note(
                f"⚠️ run_task 第 {round_i + 1} 轮**平台故障**（已重试过一次仍失败）："
                f"{type(e).__name__}: {e} —— 这一轮没跑成，交回主 agent",
                kind="run_fault",
            )
            break
        trace.dump_trace(f"run_task 第 {round_i + 1} 轮", resp["messages"],
                         role=trace.EXECUTOR)

        outcome.artifacts = list(created)  # 记账，不扫文本
        outcome.report = _last_ai_text(resp)
        outcome.rounds = round_i + 1

        # 🔴 **三件事按结构分派**（判定退休，2026-11-05）——平台只看自己看得见的：
        #
        # ① **它申报了要信息**（调了 `ask_user`）→ 这一轮到此结束
        #    ⚠️ 平台**不等它继续说**（结构 ✓ 不靠提示词让它自觉 ✓）——
        #    它调完工具还会接着往下说，那半截话不进这一轮的结论。
        if asked.get("question"):
            outcome.asked = asked["question"]
            trace.note(
                f"run_task 第 {round_i + 1} 轮：**申报要信息**（`ask_user`）→ 这一轮结束",
                kind="ask",
            )
            break

        # ② **它交了产物** → 收工。
        #    ⚠️ 平台只说"**交了东西**"这个**事实** ✓ —— "够不够、对不对"由主 agent 判
        #    （回执把交付账摆给它 ✓）。平台**不判质量**（那是判定的活，判定的毛病就在这）。
        if outcome.artifacts:
            trace.note(
                f"run_task 第 {round_i + 1} 轮：交付 {len(outcome.artifacts)} 个产物 → 收工",
                kind="delivered",
            )
            break

        # ③ **既没交、也没申报** → 推回去（照结构事实说，不编"还缺什么"）
        #    🔴 这才是"出口限制"的意义：没干完活是**它的问题**，不该转嫁给用户。
        if round_i + 1 < MAX_REACT_ROUNDS:
            nudge = nudge_text()
            trace.note(
                f"⚠️ 第 {round_i + 1} 轮**既没交付、也没申报要信息** → 推它继续（提示词见下）",
                kind="nudge",
            )
            trace.handoff("推回去", "平台", "执行器", nudge,
                          structured="结构事实：本轮无产物 ＋ 没调 `ask_user`", full=True)
            messages = list(resp["messages"]) + [HumanMessage(nudge)]
        else:
            # **显式写出这个分支**：推满了，不再推 → 下面按结构定成 incomplete
            trace.note(
                f"⚠️ 第 {round_i + 1} 轮既没交付、也没申报，且已到轮次上限 → 不再推，交给主 agent"
            )

    outcome.status = _structural_status(outcome)

    trace.note(
        f"run_task 结束 · {run_id} · skill={skill.name}：status={outcome.status} "
        f"轮次={outcome.rounds} 产物={outcome.artifacts or '（无）'}"
        + (f"｜**申报要信息**：{outcome.asked}" if outcome.asked else "")
        + (f"｜**平台故障**：{outcome.fault}" if outcome.fault else "")
        + f"（{time.time() - t0:.1f}s）",
        kind="run_end",
    )
    return outcome


def _structural_status(outcome: ExecOutcome) -> str:
    """**三态由结构定**（判定退休，2026-11-05）—— 平台只看自己**看得见的三件事**：

      - **申报了要信息**（调过 `ask_user`）→ `waiting_for_input`（走试答 → 转用户）
      - **交了产物**（`write_artifact` 记账 ✓）→ `completed`
      - **都没有** → `incomplete`（推回去；推满 3 轮交给主 agent）

    🔴 抽成函数是为了**可单测**（原来内联在 `run_task` 里，测不到）——
    这是全系统**唯一**决定"任务往哪走"的地方，必须钉住。

    ⚠️ **顺序不能反**：申报优先（它可能在交了一部分之后卡住 —— 那时该去要信息 ✓）。
    ⚠️ `completed` **只说"交了东西"这个事实**，不说"够不够/对不对"—— 那是主 agent 判的
    （回执把交付账摆给它 ✓）。平台判质量是判定时代的做法，毛病就在那。
    """
    if outcome.asked:
        return "waiting_for_input"
    if outcome.artifacts:
        return "completed"
    return "incomplete"


def _last_ai_text(resp: dict) -> str:
    """执行器的**报告** = 最后一条有内容的 AI 消息。

    ⚠️ **只用于「给主 agent 的摘要」**（`outcome.report`）—— 那里要的就是"最后说了什么"。
    ⚠️ **它不参与任何判断**（2026-11-05 起）：平台只看结构（产物账 / `ask_user`），
    "它说了什么"只**原样**进回执（标着"它说的"），由主 agent 读。
    """
    for m in reversed(resp["messages"]):
        if isinstance(m, AIMessage) and str(m.content).strip():
            return str(m.content)
    return ""
