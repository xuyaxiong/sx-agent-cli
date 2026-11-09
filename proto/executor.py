"""**Sub Agent = 可选执行者** —— 按需创建、通用、隔离上下文；一次 `run_task` 跑一趟。

## 这个文件是什么

链路里的**执行端**：主 agent 用 `delegate` 派活 → 平台在这里起一个隔离上下文的
子 agent（`create_agent` ＋ 技能指令 ＋ 交接书），跑完把 `ExecOutcome` 交回去。

三条约束：

1. **不绑 skill** —— 技能指令由主 agent 随任务传进来。加一个技能不需要动平台。
2. **用完即弃** —— 不跨轮存活、不挂起、不与用户交互。
   （状态在 `Task` 上，不在执行器上——所以"重新起一个"是无害的。）
3. **不许随便停** —— 它必须以三态之一结束：
   `completed`（任务完成）/ `waiting_for_input`（在要信息）/ `incomplete`（都没）。
   **`incomplete` → 平台把它推回去继续 react**（最多 `MAX_REACT_ROUNDS` 轮），
   而不是让用户承担它没干完的后果。

## 设计原则（不变量）

1. **出口三态由结构算**（`_structural_status`）—— 平台只看自己**看得见的三件事**：
   申报要信息（调了 `ask_user`）/ 交了产物（`write_artifact` 记账）/ 都没有。
   **不读正文、不判质量**："够不够、对不对"由主 agent 判（回执把交付账摆给它）。〔沿革 3〕
2. **产物只是"证据之一"** —— 纯分析任务可能**没有**文件产物，半成品可能**有**文件；
   ⇒ 拿"有没有文件"当"完成没完成"就是**假话**。这是平台**不判质量**的理由，
   也是出口三态只按**结构**算的理由。〔沿革 3〕
3. **判据的输入一样都不能少**（`_verdict` 的【前提】格）—— 平台**看得见的**三件结构事实
   （申报 / 产物账 / 轮次），**"没有"也要显式写**。
   ⚠️ 判定那次 **LLM 调用**退休了（〔沿革 3〕），但**这条记录还在**：用户要审计的正是
   "**给它的上下文是啥、结果又是啥、依据又是啥**" —— 换了填它的人，**输入不能少**。〔沿革 13〕
4. **状态是结构，不是文本** —— 三态用枚举（不是两个布尔值）；要信息走 `ask_user` 工具，
   不写在回复正文里；故障记 `fault_kind`，不从 `fault` 文本反解析。
5. **闭包按 run 归属**（`created` / `asked` / `_user_turn_reader`）—— 不用模块级全局：
   并行 delegate 时两个执行器会互相覆盖。

## 关键取舍

**① 故障不塞进 `report`** —— 回执里【执行器说】的来源标注是"**它说的**"，
而故障那句话是**平台说的**：塞进去正好污染"来源标注不能串"这条规矩。

**② 故障时 `status` 仍是三态里的值** —— 三态封闭、**没有第四种**；
"主 agent 该怎么做"由 `fault_kind` 决定（见回执的【平台故障】段）。

## 沿革（改过什么、为什么改）

> 编号是**引用锚**（正文里写 `〔沿革 N〕`）—— **只追加、不改号**，新条目加在**末尾**。

1. **`OutcomeStatus` 词汇表收进 `proto/handoff`**（2026-11-04 复审 N4）。原来这里和
   `_Judgement.status` 各写一遍同一串字面量，改一处就静默分叉；本文件再导出一次
   只是为了不打断既有引用。
2. **故障兜底进 `fault` 字段**（2026-11-04 复审 N2/R8）。`stream_agent` 抖两次之后
   抛到这里，`run_task` 把它接住、记成 `fault`，而不是让异常穿出去炸掉整轮
   （用户白等 3 分钟 + 一屏栈）。兑现 `docs/solution.md` §0.6 的 **N2** 承诺 ——
   它原来只在 `safe_structured` 那条路上成立。
3. **判定那套整块退休**（2026-11-05）。`_Judgement` / 规则 / 材料 / 截断 / `build_prompt`
   全删："任务结没结束"不再由一次 LLM 调用判，改成三件**平台看得见的**结构事实
   （申报要信息 / 交了产物 / 都没有）；"够不够"由主 agent 判。同一次还摘掉了
   `progress` / `missing`（判定吐的"已做到 / 还缺"）—— 判定没了来源，整条链一起摘
   （`Task` / 交接书 / 台账 / 回执）。"问完成没、不问产物生成了没"这个问法是评审人
   **C2** 的落点，也是用户明确纠正过的。
4. **`ask_user` 工具的由来**（2026-11-05）。在这之前，"我要信息"只能**写在回复正文里**，
   平台得**读文本**才知道（那是判定的活）—— 于是"要不要把执行器说的话喂给判定"
   成了死结：喂了，礼貌收尾被当成在要信息；不喂，真问题被吞。现在它**调工具**，
   平台**直接看见**两件事：它卡住了（要信息）、它要什么（`question` 里那句**原话**）。
5. **`ask_user` 返回那句话的措辞**（2026-11-05 用户指着日志改的）。原文是
   「（已登记：平台会先问主 agent，它答不上再转用户）」—— 它读起来像"平台**现在**
   要去问主 agent 了"，而**试答那一刻还没发生**（工具返回在前、试答在后）：复盘的人
   把"接下来"当成了"已经"。而且它是**规则**（对每一次都成立），不是**这一次的事实**。
   现在先说**刚刚发生的那件事**（登记为待答），再用「接下来」交代后手。
6. **`_at`：两个编号各自带名字**（2026-11-05）。用户指着日志：
   *「`R01`」和「`轮次=2`」并列，读者分不清哪个是哪个*。原来两处都写「第 N 轮」
   （一个指 run 内、一个指 run 间），结尾那行又把 `R01` 和 `轮次=` 并排放 ——
   **同一个词指两件事**，于是那两行"没头没脑"。
7. **`created` 改成工具自己记账**（2026-11-05）。原来是**扫消息文本**猜
   （`_recover_artifacts` + `preexisting` 差集）—— 既不是结构化、也不是硬机制，
   而且并发时差集还会互相覆盖。
8. **技能文件提示收成单源**（2026-11-05 消 F2）。`skill_file_prompt` 这段提示和
   `read_skill` 工具返回的原来是**两处各拼一遍、逐字重复**；"必须写 skill_name"
   那条实测教训也搬进了那个函数。
9. **日志块走同一个 `render_hop`**（2026-11-05 修）。原来日志那处是**重拼一遍**的：
   `task_text` 改了缩进、这里没跟，于是**日志里还是平的**（用户指着日志问：
   *"这里为啥不缩进"*）—— 同一份内容两处拼，形状必然漂，而**日志是给人复盘看的**，
   它漂了就会误导复盘的人。同一次还修了简写里的段名：原来写
   `f"{SECTION['skill_instructions']}{skill.name}…"` —— 段名是 `render_hop` 加的，
   于是日志里印成**两行**【技能指令】（"段名只有一处加"被开了个口子）。
10. **「问」是一条链**（2026-11-05 用户定）。原话：*"关注 谁在向谁问一个什么问题 ——
    你记录必须把这个搞清楚；谁答不上被转发给谁了；谁又答复了谁的什么问题。"*
    所以每一跳都写清「谁 → 谁：什么」。
11. **`run_end` 那行是「这次 run 的账」，不是复述**（2026-11-05）。两处改：
    ① 两个编号各自带名字（`R01 · … 轮次=2` 那种写法作废，见 `_at`）；
    ② **不再重贴那句问题** —— 它上面已经有**三处**该有的地方（`🔧 TOOL` 的申报原话 /
    `📤 交接 · 要信息` 的摘要 / 试答的【前提】），而**这一行是摘要**：摘要里突然塞一句
    132 字的原话，正是用户说的"没头没脑"。现在只留一个**指路**。
12. **`_structural_status` 抽成函数**（2026-11-05）。原来内联在 `run_task` 里、
    **测不到** —— 而这是全系统**唯一**决定"任务往哪走"的地方，必须钉住。
13. **`_verdict`：判定记录留下来**（2026-11-05）。用户原话：*"我得搞清楚对于完成没，
    **给它的上下文是啥、结果又是啥、产生结果依据又是啥** —— 这些都要能看到才能审计，
    不然我咋知道怎么调优。"* 判定那次 **LLM 调用**退休了，但**这个判断还在** ——
    走 `emit_decision` **同一个记录形状**：它**不是"交接"**（判据是"关键在于 LLM
    参与了"），但四格问的是同一件事（谁基于什么、得出了什么、接下来干什么），
    另起一套形状只会分叉。
14. **`_last_ai_text` 不参与任何判断**（2026-11-05）。平台只看结构（产物账 /
    `ask_user`）；"它说了什么"只**原样**进回执（标着"它说的"），由主 agent 读。
15. **空流按同一套故障处置**（2026-11-06 审计）。原来这里 `resp = {}` 直接往下走，
    `resp["messages"]` 抛 KeyError —— 而且**不在上面那个 `try` 里**：这一轮既没记
    `fault`、也没说人话，整个 invoke 炸穿到最外层（用户看到的是"平台故障 + 一段
    KeyError 栈"，读日志的人查不出所以然）。
16. **`MAX_WALL_S` 墙钟**（2026-11-06，外审 F9 的收尾）。单次调用有 `MODEL_TIMEOUT_S`
    （默认 300s 的**读间隔**超时）兜着，但那是**一次调用**的界 —— 三轮 × 每次两回
    尝试 × 300s，最坏仍能拖到**几十分钟**，用户那头就是"转圈转到不知道在干什么"。
    600s 是按已观测的"一轮 60–120 秒"定的（5–10 倍余量）。⚠️ 它**拦不住"一次阻塞
    调用"**（那个归 `MODEL_TIMEOUT_S`）—— 墙钟在**轮次边界**查，所以最坏是
    `MAX_WALL_S` ＋ 一次调用的最坏耗时。两条一起才是完整的闸门。
17. **故障分档：`transient` / `rejected` / `timeout`**（2026-11-06，外测 SX-07 ＋ 外审
    F9 的收尾）。原来一笼统当"基础设施抖动"：4xx（除 408/429）是上游对**这份内容**的
    确定性拒绝 —— 重试是白烧一次，"原样再派"对它就是**错的话**（该说"换措辞/改内容"）；
    `timeout` 又两样都不是（它**一直在干活**，只是没干完）。三档记成结构
    （`fault_kind`），四处引导句按它渲染；**不许从 `fault` 文本反解析**（判据一）。
18. **`run_task` 拆成四个件**（2026-11-09，第四轮外审 P1-8）。用户定：
    *"函数必须得内聚呀 不要让函数成为富函数 这样容易失去维护"*。
    它原来是 **217 行**的一个函数，里面**四件事缠在一起**：建 agent ＋ 渲染输入、
    跑一轮（墙钟 / 流式 / 三种"没跑成"）、**判出口**（三态 ＋ 推回去）、收尾（算 status ＋
    记判定 ＋ 写结束行）。⇒ 拆成：

    | 件 | 干什么 | 行数（代码） |
    |---|---|---|
    | `_open_run` | 准备：建 agent、渲染这一趟的输入、写开跑前那三条日志 | 46（30） |
    | `_run_round` | 跑**一轮**：墙钟 → 流式 → 吸收结果。**没跑成返回 `None`** | 75（56） |
    | `_decide_exit` | **判出口**：按结构看这一轮该怎么走（`"stop"` / `"again"`） | 57（38） |
    | `_close_run` | 收尾：算三态 → 记判定 → 写结束那行 | 27（17） |

    `run_task` 自己只剩 **28 行**编排（准备 → `for 轮次: 跑一轮 → 判出口` → 收尾）。
    ⚠️ **"没跑成"和"判出口"是两个函数、两种返回**：前者返回 `resp | None`
    （`None` ＝ 故障，已记进 `outcome`），后者返回动作串 —— 原来它们都是循环里的 `break`，
    读的人得自己数"这个 break 是故障还是出口"。
    ⚠️ **运行态收进 `_Run`**（`messages` / `outcome` / `created` / `asked` / `t0`）——
    拆函数就得有个地方放它们；`created` / `asked` 还是**工具的闭包**，必须和 `agent` 同生共死。
    ⚠️ **零行为变化**：注释、日志、分支顺序、`break` 的时机全部原样搬。
    守卫还是原来那些（`unit_ask_flow` 真跑 `run_task` ＋ `unit_handoff`）。
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
from proto.handoff import FaultKind, OutcomeStatus as _OutcomeStatus, render_hop
#                ↑ 状态词汇（唯一一份）· 段名/渲染（唯一一份）
from proto.handoff.decision import emit_decision  # 决策事件的发射器（归领域包）
from proto.handoff.nudge import nudge_text  # 推回去的模板
from proto.artifacts import (
    read_artifact,
    make_write_artifact,
)
from proto.skill_pack import read_skill_file, skill_file_prompt  # 技能包文件（磁盘）＋ 提示块（单源）
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

#: 🔴 **一趟的墙钟上限**（秒）—— 给**整趟**一个界：单次调用有 `MODEL_TIMEOUT_S` 兜着，
#: 但那是**一次调用**的界，叠起来最坏能拖到几十分钟。
#: ⚠️ **它拦不住"一次阻塞调用"**（那个归 `MODEL_TIMEOUT_S`）—— 墙钟在**轮次边界**查，
#: 所以最坏是 `MAX_WALL_S` ＋ 一次调用的最坏耗时。两条一起才是完整的闸门。〔沿革 16〕
MAX_WALL_S = 600

#: 每次 `run_task` 一个递增编号 —— 日志里用来**区分不同的 run**
#: （一次进程会跑很多次 run_task，没有编号就分不清哪一行属于哪一次）
_RUN_SEQ = itertools.count(1)


#: 出口三态 —— **词汇表在 `proto/handoff`**（唯一一份，见模块头设计原则 4）。
#: 这里再导出一次只是为了不打断既有引用（`ExecOutcome.status` 的标注用它）。〔沿革 1〕
OutcomeStatus = _OutcomeStatus


@dataclass
class ExecOutcome:
    """执行器的结果 —— 平台据此决定任务状态。

    ⚠️ **状态用枚举，不用两个布尔值**（用户原则 ②：决策结果要结构化）。
    两个布尔值有 4 种组合、只有 3 种合法，"矛盾组合"只能靠约定排除；
    枚举则**从类型上就闭合**。

    〔沿革 3〕原来还有 `progress` / `missing`（**判定**吐的"已做到 / 还缺"）——
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
    #: 它是**结构**：执行器调了 `ask_user` → 平台直接看见 → 走"试答 → 转用户"。〔沿革 4〕
    asked: str = ""
    #: 🔴 **平台故障**（非空 = 这一轮**根本没跑成**，不是执行器干不了）。
    #: ⚠️ **不能塞进 `report`**（那是"它说的"）—— 见模块头「关键取舍 ①」。
    #: ⚠️ 故障时 `status` 仍是 `incomplete`（三态封闭）—— 见模块头「关键取舍 ②」。〔沿革 2〕
    fault: str = ""
    #: 这一趟实际跑了多久（秒）—— `run_end` 那行一直印着它，现在**也进对象**：
    #: 超时那条交付行要说"跑到 X 分钟还没交"（说不清就不说，见 `receipt`）。
    wall_s: float = 0.0
    #: 🔴 **平台故障的"类"**（`FaultKind`：`transient` / `rejected` / `timeout` / `""`）——
    #: 四处引导句按它渲染（抖动原样再派 / 被拒换措辞 / 超时拆小）；**不许从 `fault`
    #: 文本反解析**（判据一）。与 `fault` 同生共死：每次 `run_task` 重新赋值、
    #: `settle` 覆盖写（正常跑完清空）。〔沿革 17〕
    fault_kind: FaultKind = ""


def make_ask_user(asked: dict):
    """造 `ask_user` 工具 —— 执行器**要信息**的唯一出口（**结构**，不是正文里的一句话）。

    它**调工具**，平台**直接看见**两件事：**它卡住了**（不用猜）／**它要什么**
    （`question` 里那句**原话**，不用从正文里挑哪句是问题）。
    之后：平台先让主 agent 试答（它上下文里可能就有）→ 答不上把 `question` **原样**转用户。〔沿革 4〕

    ⚠️ **这一轮到此结束**：`run_task` 看到调用就 `break`（不等它继续说 ✓ 结构 ✓）——
    它调完工具还会接着往下说，那半截话不进这一轮的结论。
    ⚠️ 和 `make_write_artifact` 一样用**闭包**（按 run 归属），不用模块级全局。
    """

    @tool
    async def ask_user(question: str) -> str:
        """你**没法往下干**了，需要一条你自己拿不到的信息 —— 用这个问。

        Args:
            question: **具体、能直接回答**的一句话（如"这节课用几课时？"）。
                一次问一件；**能自己判断的别问**（会打扰用户）。

        ⚠️ **要信息必须用它**：写在回复正文里**平台看不到**（平台不读正文）。
        """
        asked["question"] = str(question or "").strip()
        # 🔴 **说的是「这一次登记了什么」，不是一句流程说明**（〔沿革 5〕）——
        #    先说**刚刚发生的那件事**（登记为待答 ✓ 这一次的），再用「接下来」交代后手。
        #    ⚠️ 这行**只是记录** —— `run_task` 看到 `asked` 就 `break`，它不会回到执行器眼里。
        return ("**已登记为待答** —— 这一轮到此结束。"
                "接下来：平台先让主 agent 试答（它上下文里可能就有），"
                "答不上才把你这句**原话**转给用户。")

    return ask_user


def _user_turn_reader(user_turns: list[dict]):
    """造一个**只认这次交接里那几轮**的读取工具（渐进式披露第三层）。

    ⚠️ **用闭包，不用模块级全局**：并行 delegate 时两个执行器会互相覆盖 ——
    `ARTIFACT_DIR` 那种全局状态在这儿就是坑（同源教训见 `graph._merge_tasks`）。
    """
    by_n = {int(t["n"]): t for t in user_turns}

    @tool
    async def read_user_turn(turn: int) -> str:
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


def _at(run_id: str, n: int) -> str:
    """**这一轮在哪儿** —— `R01 · 内部第 2 轮`（两个编号**各自带着名字**）。

    两个数**都有用**，但必须说清各是什么（同一个词指两件事，读者就分不清 ——〔沿革 6〕）：

      - `R01` = **第几次 `run_task`**（进程内递增 —— 投递重跑 / 试答重跑都会**新开一次**）
      - `内部第 N 轮` = **这一次 run 里 react 循环第几圈**（没出口就推回去，+1）
    """
    return f"{run_id} · 内部第 {n} 轮"


@dataclass
class _Run:
    """一次 `run_task` 的**运行态** —— 拆成几个函数之后，这些得有个地方待着。

    ⚠️ **只在 `executor` 内部传**（`run_task` ↔ 下面那几个私有件），不对外 ——
    对外的结果是 `ExecOutcome`。
    ⚠️ `created` / `asked` 是**工具的闭包**写的（`make_write_artifact` / `make_ask_user`），
    所以它们必须和 `agent` 同生共死（`_open_run` 把两者一起造出来）。
    """

    skill: Skill
    run_id: str
    t0: float
    #: 每轮重入的输入 —— 推回去时往它后面追加（见 `_run_round` 的 ③）
    messages: list
    outcome: ExecOutcome
    #: 这次 run 写了哪些产物 —— **由工具自己记**（闭包，按 run 归属；见 `make_write_artifact`）。〔沿革 7〕
    created: list[str]
    #: `ask_user` 申报的内容（闭包，同 `created`）
    asked: dict


def _build_executor(user_turns: list[dict] | None,
                    created: list[str], asked: dict):
    """建**执行器**那个子 agent —— 工具表 ＋ 平台规则。

    ⚠️ 工具表在这儿拼、agent 在这儿建：`created` / `asked` 是**工具的闭包**，
    两者得绑在同一份闭包上（工具记的东西就是这一趟 run 的东西）。
    """
    tools = [
        make_write_artifact(created),
        read_artifact,
        read_skill_file,
        make_ask_user(asked),
    ]
    if user_turns:
        tools.append(_user_turn_reader(user_turns))
    return create_agent(
        get_model(),
        tools=tools,
        system_prompt=_PLATFORM_RULE,
        name="executor",
    )


def _open_run(skill: Skill, briefing: str, user_turns: list[dict] | None) -> tuple[_Run, object]:
    """**准备**：建 agent → 渲染这一趟的输入 → 把开跑前那三条日志写掉。

    返回 `(运行态, agent)` —— agent 得交回给 `run_task`（每轮都用同一个）。
    """
    created: list[str] = []
    asked: dict = {}
    agent = _build_executor(user_turns, created, asked)

    briefing_text = briefing.strip() or "（暂无 —— 你需要的任何信息都要先问，不要猜）"
    # 技能自带文件时，把**清单**（路径 + 字数）一起给执行器 —— 正文按需 `read_skill_file` 读。
    # 🔴 这段提示**和 `read_skill` 工具返回的是同一份**（单源：`skill_file_prompt`）——
    #    清单现场扫盘、没有自带文件就返回空串（一个字不加）。〔沿革 8〕
    refs = skill_file_prompt(skill.name)
    # 🔴 **走唯一那条渲染路**：交接书整块缩在【已知信息】下面 —— 哪几段属于它，
    #    一眼看得出（原来是就地写的字面量，嵌套还是平的）。〔沿革 9〕
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
        # 🔴 **走同一个 `render_hop`** —— 同一份内容两处拼，形状必然漂；而**日志是给人
        #    复盘看的**，它漂了就会误导复盘的人。〔沿革 9〕
        # ⚠️ **只有 `skill_instructions` 是简写**（不重复贴技能原文，原文在技能包里）；
        #    `known` 是**原文**，和真发出去的那份逐字相同。
        # ⚠️ **简写里不许再带一遍段名** —— 段名只有 `render_hop` 一处加（〔沿革 9〕）。
        render_hop("task_text", {
            "skill_instructions": f"{skill.name}"
                                  f"（instructions {len(skill.instructions)} 字，"
                                  f"原文见该技能包的 SKILL.md）",
            "known": briefing_text,
        }),
        kind="executor_briefing",
    )
    return _Run(skill=skill, run_id=run_id, t0=time.time(),
                messages=[HumanMessage(task_text)], outcome=ExecOutcome(),
                created=created, asked=asked), agent


def _record_fault(run: _Run, rounds: int, fault: str, kind: FaultKind, note: str) -> None:
    """记一次「**这一轮没跑成**」—— 三处（墙钟 / 流式抛异常 / 流一步都没吐）共用这一套。

    🔴 **必须走一处**：`fault` / `fault_kind` / `rounds` 三个字段**同生共死** ——
    漏写一个就是"有故障原因但没有类"，而四处引导句按 `fault_kind` 渲染
    （缺了它退回兜底那句 = 把"换措辞"说成"原样再派"，**是假话**）。
    ⚠️ 所以 `kind` 是**必填位置参数**，不给默认值 —— 想漏都漏不掉。
    """
    outcome = run.outcome
    outcome.fault = fault
    outcome.fault_kind = kind
    outcome.rounds = rounds
    trace.note(note, kind="run_fault")


async def _run_round(run: _Run, agent, round_i: int) -> dict | None:
    """跑**一轮**：墙钟 → 流式跑 → 把这一轮的结果吸收进 `outcome`。

    返回那一轮的 `state`（`resp`）；**没跑成返回 `None`** —— 三种"没跑成"（墙钟用完 /
    流式抛异常 / 流一步都没吐）**故障已经记进 `outcome`**，调用方看到 `None` 直接停
    （三者的处置是同一套：交回主 agent，由它决定原样再派还是换措辞）。
    """
    outcome = run.outcome
    # 🔴 **整趟的墙钟**：在**轮次边界**查（拦不住单次阻塞调用，那个归
    #    `MODEL_TIMEOUT_S`）—— 超了就**不再开新一轮**，按故障交回主 agent。
    #    ⚠️ 它是**故障**不是"执行器干不了"：它一直在干活，只是没干完（`timeout` 那档）。
    if time.time() - run.t0 > MAX_WALL_S:
        _record_fault(
            run, round_i, f"整趟超过墙钟上限（{MAX_WALL_S}s）", "timeout",
            f"⚠️ {run.run_id} **时间用完了**（已跑 {time.time() - run.t0:.0f}s > 上限 {MAX_WALL_S}s）"
            f" → 不再开新一轮，交回主 agent（原样再委派可以；老超时就拆小）",
        )
        return None
    # 不传 config —— 传了执行器会变成独立根图。
    # `stream_agent` 边跑边录（每个字都写进日志）—— 一次 LLM 调用那几十秒不再黑屏。
    # ⚠️ `who` 带上技能名：多个执行器**并行**时，流式行会交错，得能分辨是谁。
    # `role` 是**逻辑**（展示层按它归版式），`who` 只是给人看的名字 —— 两者别混。
    resp = {}
    try:
        async for chunk in trace.stream_agent(agent, {"messages": run.messages}, None,
                                              f"执行器·{run.skill.name}", role=trace.EXECUTOR):
            resp = chunk
    except Exception as e:  # noqa: BLE001
        # 🔴 **基础设施抖动不该炸整轮**：`stream_agent` 内部已经重试过一次，
        #    **两次都失败才走到这** —— 这一轮记成"**平台故障**"（不是执行器干不了），
        #    把决定权交回主 agent。〔沿革 2〕
        # ⚠️ 不在这里重跑：`run_task` 一轮就是 60–120 秒，再重跑是拿用户的时间赌。
        # 🔴 **故障分类从结构取**：4xx（除 408/429）是上游对**这份内容**的确定性拒绝
        #    —— 重试层已经不重试它了（`trace.upstream_rejected`），这里把"类"记下来，
        #    四处引导句据此说"换措辞"而不是"原样再派"。〔沿革 17〕
        _kind: FaultKind = "rejected" if trace.upstream_rejected(e) else "transient"
        _note = (
            f"⚠️ {_at(run.run_id, round_i + 1)}**上游拒绝了这次请求**（4xx，未重试 ——"
            f" 重发同一份内容会再被拒）：{type(e).__name__}: {e}"
            f" —— 这一轮没跑成，交回主 agent 换措辞/改内容"
            if _kind == "rejected" else
            f"⚠️ {_at(run.run_id, round_i + 1)}**平台故障**（已重试过一次仍失败）："
            f"{type(e).__name__}: {e} —— 这一轮没跑成，交回主 agent"
        )
        _record_fault(run, round_i + 1, f"{type(e).__name__}: {e}", _kind, _note)
        return None
    if not resp:
        # 🔴 **流一步都没吐** → 按**同一套故障处置**：这一轮没跑成，交回主 agent
        #    （同上面 `except`）。⚠️ 主 agent 那边有同类兜底（`graph.node_agent` 的
        #    `stream_empty`），执行器这条原来没有。〔沿革 15〕
        _record_fault(
            run, round_i + 1, "流式一步都没吐（stream 为空）", "transient",
            f"⚠️ {_at(run.run_id, round_i + 1)}**流式一步都没吐** → 这一轮没跑成，"
            f"交回主 agent（同平台故障处置）",
        )
        return None
    trace.dump_trace(_at(run.run_id, round_i + 1), resp["messages"],
                     role=trace.EXECUTOR)

    outcome.artifacts = list(run.created)  # 记账，不扫文本
    outcome.report = _last_ai_text(resp)
    outcome.rounds = round_i + 1
    return resp


def _decide_exit(run: _Run, resp: dict, round_i: int) -> str:
    """**判出口**：这一轮跑完了，按结构看下一步怎么走。

    返回 `"stop"`（有出口了 —— 申报要信息 / 交了产物）／`"again"`（推回去接着跑，
    或已到轮次上限、推不动了）。

    🔴 **出口是"结构算出来的"**（判定退休，见模块头设计原则 1）：只看平台自己看得见的
    三件事 —— 调没调 `ask_user` / 记没记账 / 有没有产物。**不读正文、不判质量**。〔沿革 3〕

    ⚠️ 推回去时**改的是 `run.messages`**（下一轮的输入）—— 所以它拿的是 `run` 本身。
    """
    outcome = run.outcome
    # 🔴 **三件事按结构分派** —— 平台只看自己看得见的（判定退休，见模块头设计原则 1）：〔沿革 3〕
    #
    # ① **它申报了要信息**（调了 `ask_user`）→ 这一轮到此结束
    #    ⚠️ 平台**不等它继续说**（结构 ✓ 不靠提示词让它自觉 ✓）——
    #    它调完工具还会接着往下说，那半截话不进这一轮的结论。
    if run.asked.get("question"):
        outcome.asked = run.asked["question"]
        # 🔴 **「问」是一条链，每一跳都写清「谁 → 谁：什么」**〔沿革 10〕——
        #    这一跳是**链头**：执行器 → 平台。摘要行走 `trace.handoff`
        #    （和别的交接**同一套形状** ✓），问题原话由上面那条 `🔧 TOOL` 给全。
        trace.handoff("要信息", "执行器", "平台", outcome.asked,
                      structured="`ask_user(question=…)` 的参数原话"
                                 "（**全文见上面那条 `🔧 TOOL`**，这里不重贴）")
        trace.note(f"{_at(run.run_id, round_i + 1)}：**申报要信息** → 这一轮结束", kind="ask")
        return "stop"

    # ② **它交了产物** → 收工。
    #    ⚠️ 平台只说"**交了东西**"这个**事实** ✓ —— "够不够、对不对"由主 agent 判
    #    （回执把交付账摆给它 ✓）。平台**不判质量**（那是判定的活，判定的毛病就在这）。
    if outcome.artifacts:
        trace.note(
            f"{_at(run.run_id, round_i + 1)}：交付 {len(outcome.artifacts)} 个产物 → 收工",
            kind="delivered",
        )
        return "stop"

    # ③ **既没交、也没申报** → 推回去（照结构事实说，不编"还缺什么"）
    #    🔴 这才是"出口限制"的意义：没干完活是**它的问题**，不该转嫁给用户。
    if round_i + 1 < MAX_REACT_ROUNDS:
        nudge = nudge_text()
        trace.note(
            f"⚠️ {_at(run.run_id, round_i + 1)}**既没交付、也没申报要信息**"
            f" → 推它继续（提示词见下）",
            kind="nudge",
        )
        trace.handoff("推回去", "平台", "执行器", nudge,
                      structured="结构事实：本轮无产物 ＋ 没调 `ask_user`", full=True)
        run.messages = list(resp["messages"]) + [HumanMessage(nudge)]
    else:
        # **显式写出这个分支**：推满了，不再推 → 下面按结构定成 incomplete
        trace.note(
            f"⚠️ {_at(run.run_id, round_i + 1)}既没交付、也没申报，且已到轮次上限"
            f" → 不再推，交给主 agent"
        )
    return "again"


def _close_run(run: _Run) -> ExecOutcome:
    """**收尾**：算三态 → 记判定 → 写结束那行。"""
    outcome = run.outcome
    outcome.wall_s = time.time() - run.t0
    outcome.status = _structural_status(outcome)

    # 🔴 **「任务完成没」的判定记录** —— 判定那次 **LLM 调用**退休了，但**这个判断还在**，
    #    而且它正是用户要审计的那一处（为什么记、记什么，见 `_verdict` 的 docstring）。〔沿革 13〕
    emit_decision(**_verdict(outcome, run.run_id))

    # 🔴 **这行是「这次 run 的账」，不是复述**〔沿革 11〕——
    #    ① 两个编号各自带名字（`_at`）；② **不再重贴那句问题**：它上面已经有该有的
    #    三处（`🔧 TOOL` 的申报原话 / `📤 交接 · 要信息` 的摘要 / 试答的【前提】），
    #    而**这一行是摘要** —— 只留一个**指路**（"它要的那句见上面 `ask_user`"）。
    # ⚠️ 故障那一档的**说法按 `fault_kind` 取**（三档各有各的处置，见 `handoff.FaultKind`）——
    #    从对象取，不在这里现编；`""` 走默认那句（没故障时压根不印）。
    _fault_label = {"rejected": "上游拒绝了这次请求",
                    "timeout": "时间用完了"}.get(outcome.fault_kind, "平台故障")
    trace.note(
        f"{run.run_id} · {run.skill.name} **结束**：status={outcome.status}"
        f"（内部 {outcome.rounds} 轮，产物 {len(outcome.artifacts)} 个，"
        f"{outcome.wall_s:.1f}s）"
        + ("｜它要的那句见上面 `ask_user`" if outcome.asked else "")
        + (f"｜**{_fault_label}**：{outcome.fault}" if outcome.fault else ""),
        kind="run_end",
    )
    return outcome


async def run_task(skill: Skill, briefing: str = "", user_turns: list[dict] | None = None) -> ExecOutcome:
    """通用执行器：干活、**判定出口**、没出口就推它继续。

    Args:
        skill: 专业技能包（指令从这里来）。
        briefing: 这次交给它的全部已知 —— 由 `agent.render_briefing(task)` 渲染。
            平台**不解析、不抽取、不比对**，原样带给执行器。
            ⚠️ 名字是 briefing 不是 given：它是**整份交接**（任务 / 上次做到哪 / 历次答复 /
            已产出产物 / 可读产物），不是"这一轮的增量"—— 执行器用完即弃，每次都是重入。
            ⚠️ **可读产物清单也由它承载**（原来 `run_task` 另拼一个 `artifacts` 参数、
            两处都渲染 → 执行器看到两遍；现在只有一个来源）。
        user_turns: 用户原话（`[{"n": 1, "text": "..."}]`）—— 只给 `read_user_turn` 工具用。
            **交接书里只渲染清单**（每轮一行、开头几十字），全文按需取：
            用户可能粘长文，一上来全加载就把执行器的上下文塞满了（渐进式披露）。

    Returns:
        `ExecOutcome` —— 平台据此定任务状态（**不再靠"产物库空不空"猜**）。

    ⚠️ **它自己只做编排**（2026-11-09 拆）：准备 → `for 轮次: 跑一轮 → 判出口` → 收尾。
    四件事各在一个私有件里（`_open_run` / `_run_round` / `_decide_exit` / `_close_run`）——
    原来是一个 217 行的函数，**"跑一轮"和"判出口"和"写日志"缠在一起**。〔沿革 18〕
    """
    run, agent = _open_run(skill, briefing, user_turns)
    for round_i in range(MAX_REACT_ROUNDS):
        resp = await _run_round(run, agent, round_i)
        if resp is None:  # 没跑成（墙钟 / 异常 / 空流）—— 故障已经记进 outcome
            break
        if _decide_exit(run, resp, round_i) == "stop":
            break
    return _close_run(run)


def _structural_status(outcome: ExecOutcome) -> str:
    """**三态由结构定** —— 平台只看自己**看得见的三件事**：

      - **申报了要信息**（调过 `ask_user`）→ `waiting_for_input`（走试答 → 转用户）
      - **交了产物**（`write_artifact` 记账 ✓）→ `completed`
      - **都没有** → `incomplete`（推回去；推满 `MAX_REACT_ROUNDS` 轮交给主 agent）

    🔴 抽成函数是为了**可单测**（原来内联在 `run_task` 里，测不到）——
    这是全系统**唯一**决定"任务往哪走"的地方，必须钉住。〔沿革 12〕

    ⚠️ **顺序不能反**：申报优先（它可能在交了一部分之后卡住 —— 那时该去要信息 ✓）。
    ⚠️ `completed` **只说"交了东西"这个事实**，不说"够不够/对不对"—— 那是主 agent 判的
    （回执把交付账摆给它 ✓）。平台判质量是判定时代的做法，毛病就在那。
    """
    if outcome.asked:
        return "waiting_for_input"
    if outcome.artifacts:
        return "completed"
    return "incomplete"


def _verdict_reason(outcome: ExecOutcome) -> tuple[str, str]:
    """判定的**依据**（`why`）与**下一步**（`nxt`）—— 两格文案的唯一来源。

    ⚠️ **顺序不能反**：申报优先（它可能在交了一部分之后卡住 —— 那时该去要信息 ✓）。
    ⚠️ 故障**盖在上面**：故障时 `status` 仍是三态里的值（**三态封闭，没有第四种**），
    但"这一轮根本没跑成"与"执行器干不了"是两回事，所以依据和下一步都得说故障。
    """
    if outcome.asked:
        why = "它**申报了要信息**（顺序：申报优先 → 交了产物 → 都没有）"
        nxt = ("登记待答 → 平台强制**主 agent 试答**（答得上就带回去重跑，"
               "答不上**原样**转给用户）")
    elif outcome.artifacts:
        why = "它**交了产物**（同一顺序；本轮**没申报**）"
        nxt = ("**回执给主 agent** —— 由它判「够不够」（平台不判质量；"
               "判据以**技能声明的交付标准**为准）")
    else:
        why = "**既没申报、也没交产物**（顺序里没有命中的那一条）"
        nxt = ("**交回主 agent**（推满轮次仍没出口）—— 它看回执定："
               "换个说法**重新委派** / 向用户说明")
    if outcome.fault:
        # 🔴 故障时 `status` 仍是三态里的值（**三态封闭，没有第四种**），但**这一轮根本没跑成** ——
        #    与"执行器干不了"是两回事，所以依据和下一步都得说故障（回执那边同理，见 `receipt`）。
        # 🔴 **三档故障三种处置**：见 `handoff.FaultKind` —— rejected（4xx）是上游对
        #    **这份内容**的拒绝，"原样再派"会再被拒。〔沿革 17〕
        if outcome.fault_kind == "rejected":
            why = (f"**上游拒绝了这次请求**（4xx，不是抖动）：{outcome.fault}"
                   f" —— 这一轮**根本没跑成**，不是执行器干不了")
            nxt = ("**交回主 agent** —— **原样重发同一份内容会再被拒**："
                   "换措辞/改内容再 `delegate` 一次；改不动就向用户说明这一段被拦了")
        elif outcome.fault_kind == "timeout":
            why = (f"**时间用完了**（跑满 {MAX_WALL_S}s 墙钟）：{outcome.fault}"
                   f" —— 它**一直在干活**，只是这一趟没干完，不是它干不了")
            nxt = ("**交回主 agent** —— 原样再 `delegate` 一次可以；"
                   "**老超时就说明活太大**，拆小一点再委派")
        else:
            why = f"**平台故障**（{outcome.fault}）—— 这一轮**根本没跑成**，不是执行器干不了"
            nxt = "**交回主 agent** —— 原样再 `delegate` 一次即可（不用改措辞、不用改参数、不用问用户）"
    return why, nxt


def _verdict(outcome: ExecOutcome, run_id: str) -> dict:
    """「任务完成没」这条**判定记录**的四个字段（纯函数 —— 可单测，同 `_structural_status`）。

    🔴 **为什么要记**（用户原话见〔沿革 13〕）：**"给它的上下文是啥、结果又是啥、
    依据又是啥"都要能看到才能审计**，不然没法调优。判定那次 **LLM 调用**退休了
    （三态改由结构定），但**这个判断还在** —— 记录形状不变
    （前提 / 问题 / 结果 / 下一步，见 `handoff.decision`），
    只是填它的人从 LLM 换成了平台的结构规则：

    | 格 | 这条记录里是什么 |
    |---|---|
    | **前提** | 平台**看得见的**三件结构事实（申报 / 产物账 / 轮次）—— 判据的**输入**，**包括"没有"** |
    | **结果** | 三态 ＋ **依据**（命中的是哪一条 —— 这就是"产生结果的依据"） |
    | **下一步** | 谁来接（回执给主 agent / 试答 / 交回主 agent） |

    ⚠️ **它不是"交接"**（没有 LLM 参与 —— 判据是"关键在于 LLM 参与了"），
    但**用同一套记录形状**：四格问的是同一件事（谁基于什么、得出了什么、接下来干什么），
    另起一套形状只会分叉。

    ⚠️ **它不重复上面那些"这一轮出口"的 note**：那些是**每轮的账**（只报命中的那条），
    这条是**整个 run 的判定**（三件事实全摆出来）—— 审计要的正是后者：
    只看到"交付了 1 个产物"，看不到"它**没申报**"。
    """
    why, nxt = _verdict_reason(outcome)
    return {
        "who": f"任务完成没（平台按结构定）· {_at(run_id, outcome.rounds)}",
        # 🔴 **这一格的作者是平台**（不是 LLM）—— 它决定那格用【平台判定】（**不标来源**）
        #    而不是【结果】（标着"它说的 —— LLM 的输出"）。标错了就是假话。
        "result_by": "platform",
        "q": "这个任务完成没？（三态**由结构定** —— 平台只看上面那三件事实，"
             "**不读正文、不判质量**）",
        "given": [
            "**申报要信息**（`ask_user`）："
            + ("**有** —— 原话见上面那条 `ask_user` 的参数" if outcome.asked
               else "**没有**（本轮没调它）"),
            "**交付产物**（`write_artifact` 记的账）："
            + (f"**{len(outcome.artifacts)} 个** —— {'、'.join(outcome.artifacts)}"
               if outcome.artifacts else "**没有**（本轮没调它）"),
            f"**轮次**：内部第 {outcome.rounds} 轮（上限 {MAX_REACT_ROUNDS}）",
        ],
        "result": [f"status = **{outcome.status}**", f"依据：{why}"],
        "nxt": nxt,
    }


def _last_ai_text(resp: dict) -> str:
    """执行器的**报告** = 最后一条有内容的 AI 消息。

    ⚠️ **只用于「给主 agent 的摘要」**（`outcome.report`）—— 那里要的就是"最后说了什么"。
    ⚠️ **它不参与任何判断**：平台只看结构（产物账 / `ask_user`），"它说了什么"只
    **原样**进回执（标着"它说的"），由主 agent 读。〔沿革 14〕
    """
    for m in reversed(resp["messages"]):
        if isinstance(m, AIMessage) and str(m.content).strip():
            return str(m.content)
    return ""
