"""Sub Agent = 可选执行者（按需创建、通用、隔离上下文）。

三条约束：
1. **不绑 skill** —— 能力指令由主 agent 随任务传进来。加一个技能不需要动平台。
2. **用完即弃** —— 不跨轮存活、不挂起、不与用户交互。
   （状态在 `Task` 上，不在执行器上——所以"重新起一个"是无害的。）
3. **不许随便停** —— 它必须以三态之一结束：
   `completed`（任务完成）/ `waiting_for_input`（在请求信息）/ `incomplete`（都没）。
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
from pydantic import BaseModel, Field

from proto import trace
from proto.config import get_model
from proto.handoff import OutcomeStatus as _OutcomeStatus  # 状态词汇（唯一一份）
from proto.handoff.judgement import (  # 判定：schema + 输入模板（搬去 handoff 包）
    _Judgement,
    _judge_material,
    build_prompt,
)
from proto.handoff.nudge import nudge_text  # 推回去的模板
from proto.llm import safe_structured
from proto import artifacts as artifacts_mod  # 产物领域（唯一入口是它的读 API）
from proto.artifacts import (
    read_artifact,
    store_artifact,
    make_write_artifact,
)
from proto.skill_pack import read_skill_file, skill_file_index_lines  # 技能包文件（磁盘）
from proto.skills import Skill  # 技能领域

#: 平台约定 —— 尽量少。技能作者写什么都不会破坏它，因为它不要求作者做任何事。
_PLATFORM_RULE = (
    "你是任务执行者。按下面的能力指令完成任务。\n"
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
    "  你猜的那个值会变成一份**基于错误前提的成品**，而用户不知道 —— 那比多问一句糟得多。\n"
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

    ⚠️ **`progress` / `missing` 必须一路传到主 agent**（用户原则 ①：信息交接要传细节），
    不能只在"推回去"那一步用。否则主 agent 只知道"没做成"，
    不知道"做到什么程度、还缺什么"，就没法决定是**换个说法重派**还是**向用户说明**。
    （这个坑踩过一次：failed 分支一开始只传了 `report`。）
    """

    #: 三态之一 —— 与 `Task.stage` **前三个同名**（`completed` / `waiting_for_input`），
    #: 唯一映射是 `incomplete → failed`。**没有第四种**。
    status: OutcomeStatus = "incomplete"
    artifacts: list[str] = field(default_factory=list)
    report: str = ""
    #: 实际 react 了几轮
    rounds: int = 0
    #: 判定给出的：**已经做到什么程度**（非 completed 时有）
    progress: str = ""
    #: 判定给出的：**还缺什么**（incomplete 时有）
    missing: list[str] = field(default_factory=list)


def _judge(skill: Skill, arts: list[str], material: str, briefing: str = "") -> _Judgement:
    """判定任务状态：`completed` / `waiting_for_input` / `incomplete`。

    ⚠️ 四个要点，**全是实测踩出来的**：

    1. **问"任务完成了没"，不是"产物生成了没"** ——
       产物只是证据之一：纯分析任务没有文件也算完成，半成品有文件也不算完成。
    2. **必须把 `instructions` 带进提示词** ——
       否则"按任务要求判断"是空话，模型看不到要求，只能凭常识猜。
       （实测：不带时，"有产物但内容是残次品"会被误判成"在问用户"。）
    3. **必须把 `briefing` 也带进去** ——
       否则判定不知道执行器**已经拿到了什么**，会把"它没问"当成"它缺"。
       （实测：给了"第1课时"却没带进去，判定说"未询问用户课时信息" → 误判成 waiting_for_input。
         **这是同一个 bug 的第二次出现**——判定看不到它需要的信息，就只能猜。）
    4. **规则要写得人能看懂** ——
       对比过五种提示词（`experiments/debug_judge_variants.py`，6 个用例）。
       ⚠️ **单次采样的分数会漂，别拿单个数字当论据**：
       2026-10-28 重跑（deepseek-v4-pro）A/B/C/E **全是 5/6**，
       只有 **F（用户模板）6/6** —— 早先记的 "A 3/6 · C 4/6 · E 6/6" 已不可复现。
       另注：该实验**有混淆**（A–E 用两个布尔 schema，F 用三态枚举），
       所以只能说"F 更好"，不能说"F 的提示词更好"。
       `criteria` 被砍的理由是**没有增量价值**（要求已在 instructions 里），不是分数低。
    """
    # ⚠️ 判定**输入**只记构成，不逐字复述 —— 里面既有产物正文、又有技能原文，两样都另有出处。
    # 这条构成摘要现在由**调用方**（`run_task` 的轮次循环）并进那条「决策记录」的**前提**里 ——
    # 用户 2026-11-03：一次决策就该是**一条**记录（前提 → 问题 → 结果 → 下一步），
    # 不是两条隔着一整次 LLM 调用的 note。
    # 提示词怎么拼在 `proto/handoff/judgement.py`（schema 与模板放一起）
    prompt = build_prompt(skill.instructions, arts, material, briefing)
    # 判定这一跳**不逐字记 prompt**（内含产物正文，另有出处）—— 但交了多少、从哪来要记。
    trace.handoff("判定输入", "平台", "判定", prompt,
                  structured=f"_Judgement 出参；material {len(material)} 字（逐字不记，见上一行构成）")
    return safe_structured(
        _Judgement,
        prompt,
        # 两次都失败时保守当作「没完成、也不问用户」→ 会让它再 react 一轮
        _Judgement(status="incomplete", reason="判定调用失败，保守处理"),
        tag="run_task → 判定任务状态",
        kind="judge",
        # 🔴 判定 prompt 里嵌着**产物正文**（`_judge_material` 喂的）和**技能原文**
        # （【任务要求】那段）—— 两样都另有出处（产物落盘 / `evals/skills/`），
        # 日志只记上一行"判定输入"的构成，不逐字复述（用户要求）。
        log_prompt=False,
    )


def _material_note(arts: list[str]) -> str:
    """判定那份「实际执行结果」**是从哪取的** —— 有产物取产物正文，没有才取散文。

    见 `judgement._judge_material` 的表格：**两条路的取材不同，别合并**
    （有产物时把散文也喂进去，实测会把"需要调整吗？"这类礼貌收尾判成"在要信息"）。
    """
    return (
        "、".join(
            f"{a}（{len(artifacts_mod.get(a)['content'])} 字）"
            for a in arts
            if artifacts_mod.has(a)
        )
        or "执行器散文（无产物）"
    )


def _judge_next(status: str, outcome: Any, round_i: int) -> str:
    """判定结果 → **平台接下来实际走哪条路**。

    ⚠️ 这三条就是 `run_task` 出口那段**三态枚举**的穷举，措辞跟着代码走 ——
    看日志的人判完立刻知道"所以接下来会发生什么"，不用去读源码。
    """
    if status == "completed":
        # ⚠️ **两个子出口**（`agent.delegate` 里分的）：有产物 vs 成果只在回复正文里 ——
        # 后者的回执会多带一句"**原样转达，不要改写**"，措辞别在这儿写反了。
        return (
            f"回执给主 agent（交付 {len(outcome.artifacts)} 个产物）→ 由它转达用户"
            if outcome.artifacts
            else "回执给主 agent（**没有产物**，成果在它的回复正文里）→ 由它**原样转达**用户"
        )
    if status == "waiting_for_input":
        return "登记待答 → 平台强制主 agent 试答 → 答不上就**原样透传**给用户"
    if round_i + 1 < MAX_REACT_ROUNDS:
        return f"推回去让它继续（第 {round_i + 2}/{MAX_REACT_ROUNDS} 轮，带上「还缺」逐条）"
    return "推的轮数用完了 → 交给用户（没干完、也没说要什么）"


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
        out = [f"【用户原话 · 第 {turn} 轮】", str(t.get("text") or "")]
        after = str(t.get("after") or "").strip()
        if after:
            out += [
                "",
                "—— **它在回答的上一句**（可能是主 agent 说的，也可能是平台把执行器"
                "自己的提问透传给用户的那条）：",
                after,
            ]
        return "\n".join(out)

    return read_user_turn


def run_task(skill: Skill, briefing: str = "", user_turns: list[dict] | None = None) -> ExecOutcome:
    """通用执行器：干活、**判定出口**、没出口就推它继续。

    Args:
        skill: 专业能力包（指令从这里来）。
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
    created: list[str] = []
    tools = [make_write_artifact(created), read_artifact, read_skill_file]
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
        refs = (
            f"\n\n【技能自带文件】要用哪一份就用 `read_skill_file` 读全文 ——"
            f"**`skill_name` 传「{skill.name}」**，`path` 照抄下面的清单"
            "（**不要凭文件名猜内容**）：\n" + "\n".join(have)
        )
    task_text = f"【能力指令】\n{skill.instructions}{refs}\n\n【已知信息】\n{briefing_text}"

    run_id = f"R{next(_RUN_SEQ):02d}"
    trace.section(f"run_task · {run_id} · skill={skill.name}（通用执行器，隔离上下文）",
                  kind="run_begin")
    # 🔴 **能力指令按名引用，不逐字复述**（用户要求：读了 skill 只写"读了啥、谁读的"）——
    # 技能原文是静态资产、另有落盘文件；日志里真正要留的是**这次的交接内容**。
    # 交接关键日志：这一跳交了多少、有没有结构化对象（正文在下面那个 block 里）
    trace.handoff("交接书", "平台", "执行器", briefing_text, structured="Task → render_briefing")
    trace.block(
        "发给执行器的内容（能力指令按名引用）",
        f"【能力指令】{skill.name}"
        f"（instructions {len(skill.instructions)} 字，原文见该技能包的 SKILL.md）\n\n"
        f"【已知信息】\n{briefing_text}",
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
        for chunk in trace.stream_agent(agent, {"messages": messages}, None,
                                        f"执行器·{skill.name}", role=trace.EXECUTOR):
            resp = chunk
        trace.dump_trace(f"run_task 第 {round_i + 1} 轮", resp["messages"],
                         role=trace.EXECUTOR)

        outcome.artifacts = list(created)  # 记账，不扫文本
        outcome.report = _last_ai_text(resp)
        outcome.rounds = round_i + 1

        # 判定看的「实际执行结果」= 产物正文（如有）+ **所有轮次**的 AI 产出，
        # 每一项各自保头保尾、显式标注省略（见 `_judge_material`）。
        material = _judge_material(outcome.artifacts, resp)

        j = _judge(skill, outcome.artifacts, material, briefing)
        # 进度与缺失项**记进 outcome**，让所有出口都能带出去（不只"推回去"那一步）
        # ⚠️ 过滤空串：实测模型在 waiting_for_input 时也会填 missing，填了个 `['']`
        outcome.status = j.status
        outcome.progress = (j.progress or "").strip()
        outcome.missing = [x.strip() for x in (j.missing or []) if x and x.strip()]
        # 🔴 **一次决策 = 一条记录**（用户 2026-11-03）：前提 → 问题 → 结果 → 下一步。
        # 原来前提（`_judge` 里那条 note）和结果（这里）是**两条**，中间隔着这次 LLM 调用 ——
        # 读的人得自己拼，而"前提"正是判错时要第一个看的东西。
        trace.decision(
            f"判定（LLM）· {run_id} 第 {round_i + 1} 轮",
            q="这个任务完成了没？没完成的话，是**在要信息**（waiting_for_input）还是**没干完**（incomplete）？",
            given=[
                f"技能 {skill.name}（instructions {len(skill.instructions)} 字，按名引用）",
                f"已知信息 {len(briefing.strip())} 字（委派时给的）",
                f"材料 {_material_note(outcome.artifacts)}｜"
                f"判定看到 {material.count('【产出')} 段产出 + {len(outcome.artifacts)} 个产物"
                # ⚠️ 总数**另起一句**（`｜材料共 N 字`），不挂在"N 个产物"后面 ——
                #    原来写的是"…+ 1 个产物（4111 字）"，语法上那 4111 像是**产物的字数**，
                #    而产物其实是 5487 字、判定看到 4000 字 —— 同一个东西三个数，
                #    读的人只会觉得自相矛盾（2026-11-04 审核）。
                #    ⚠️ 它**不是**产物正文的账（那个在 `✂️ 截断` 那行）：这里含标签行与截断标记。
                f"｜材料共 {len(material)} 字",
            ],
            # ⚠️ **日志不截断**（`trace.render_message` 那条契约）：判定理由**只写日志**，
            # 它要是被切了，事后复盘误判就没得看了。
            result=[
                f"status = **{j.status}**",
                f"理由：{j.reason or '（无）'}",
                f"进度：{j.progress or '（无）'}",
                "还缺：" + ("（无）" if not outcome.missing
                          else "\n" + "\n".join(f"- {x}" for x in outcome.missing)),
            ],
            # 下一步＝平台**按这个结果实际走的那条路**（和下面那段分支一一对应）
            nxt=_judge_next(j.status, outcome, round_i),
        )

        if j.status in ("completed", "waiting_for_input"):
            break
        # 剩下只有 incomplete —— **穷举完了，没有第四种**

        # 🔴 **incomplete → 不让它停**，推回去继续。
        #    这才是"出口限制"的意义：执行器没干完活是**它的问题**，不该转嫁给用户。
        #    判定给了 `missing` 就**精确告诉它缺什么**，比笼统一句"你还没做完"有效。
        if round_i + 1 < MAX_REACT_ROUNDS:
            nudge = nudge_text(j)
            trace.note(f"⚠️ 第 {round_i + 1} 轮 incomplete → 推它继续（提示词见下）",
                       kind="nudge")
            trace.handoff("推回去", "平台", "执行器", nudge,
                          structured=f"_Judgement(status=incomplete) → nudge_text", full=True)
            messages = list(resp["messages"]) + [HumanMessage(nudge)]
        else:
            # **显式写出这个分支**：推满了，不再推 → outcome 保持 incomplete → delegate 判 failed
            trace.note(f"⚠️ 第 {round_i + 1} 轮 incomplete 且已到轮次上限 → 不再推，交给主 agent")

    trace.note(
        f"run_task 结束 · {run_id} · skill={skill.name}：status={outcome.status} "
        f"轮次={outcome.rounds} 产物={outcome.artifacts or '（无）'}"
        f"{'　（多轮）' if outcome.rounds > 1 else ''}"
        f"（{time.time() - t0:.1f}s）",
        kind="run_end",
    )
    return outcome


def _last_ai_text(resp: dict) -> str:
    """执行器的**报告** = 最后一条有内容的 AI 消息。

    ⚠️ **只用于「给主 agent 的摘要」**（`outcome.report`）—— 那里要的就是"最后说了什么"。

    🔴 **绝不要拿它当判定的输入**：出口循环里消息是**累积**的，
    只取最后一条会让判定看不到前面几轮的产出。
    判定要的是**全部轮次** → 用 `_judge_material()`。
    （这个错犯过，见 `_judge_material` 的 docstring。）
    """
    for m in reversed(resp["messages"]):
        if isinstance(m, AIMessage) and str(m.content).strip():
            return str(m.content)
    return ""
