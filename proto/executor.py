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
from typing import Literal

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from proto import trace
from proto.config import get_model
from proto.llm import safe_structured
from proto.artifacts import (  # 产物领域
    ARTIFACTS,
    read_artifact,
    store_artifact,
    write_artifact,
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

#: 判定提示词里，**每一项**（一条 AI 产出 / 一个产物）的字符预算。
#: 超了就**保头保尾 + 显式标注省略** —— 不是简单截尾。
#:
#: 🔴 **这个数是被实测调出来的，原来是 800，是错的。**
#: `experiments/debug_clip_marker.py` 的预算扫描（3 次采样，产物 3112 字）：
#:
#: | 预算 | 截断？ | 完整产物→completed | 真占位符→incomplete |
#: |---|---|---|---|
#: | 800（原值） | 是 | 3/3 | **0/3** |
#: | 1600 | 是 | 3/3 | 1/3 |
#: | 2400 | 是 | 3/3 | 2/3 |
#: | **4000** | 否 | 3/3 | **3/3** |
#:
#: **单调** —— 判定判得准不准，基本取决于它能不能看到全文。
#: 800 字时占位符正好落在被截掉的中段，判定既误判完整产物、又漏掉真缺陷。
#:
#: ⚠️ **但要清醒**：这只把门槛往后挪了。产物再长（48k 课件）还是会截断，
#: 截断之后判定就**没有能力**验证完整性 —— 那时它给的"完成"是**推测**，不是判断。
#: 所以：① 这个数是**可调**的，按真实产物长度定；② 完整性最终要靠**产物落盘 + 人工审计**
#: （`evals/` 会把产物原文写到 `runs/<tag>/artifacts/`）。
JUDGE_ITEM_BUDGET = 4000

#: 「实际执行结果」整段的字符上限。超出时砍掉排在后面的项，并**显式说明少了几项**。
#: 与 `JUDGE_ITEM_BUDGET` 同步放大（约 4 项的量）。
JUDGE_TOTAL_BUDGET = 16000

#: 保头保尾时，头部占的比例（剩下给尾部）。
_CLIP_HEAD_RATIO = 0.6

#: **平台截断的标记** —— 措辞要顶得住"被当成作者占位符"。
#:
#: ⚠️ **别说"内容完整"** —— 平台**不知道**内容完不完整，那是在替判定下结论。
#: 实测（`experiments/debug_clip_marker.py` 变体 B）：光把标记写成"不代表内容缺失"，
#: 完整产物那一列从 0/3 修到 3/3，但**真占位符那一列从 3/3 塌到 0/3** ——
#: 判定开始接受真正残缺的产物。**用一个假阴性换了个假阳性。**
#:
#: 所以措辞只说**平台知道的事实**：这部分你没看到；别仅凭"看不到"下结论；
#: 但在看得到的地方发现缺陷，照常判。
_CLIP_MARK = (
    "……【平台截断：中间 {n} 字因提示词长度限制未展示给你。**你无法看到这部分**，"
    "所以不要仅凭「看不到」判未完成；但若在**看得到**的部分发现占位符、待补充、半成品，照常判未完成】……"
)

#: 每次 `run_task` 一个递增编号 —— 日志里用来**区分不同的 run**
#: （一次进程会跑很多次 run_task，没有编号就分不清哪一行属于哪一次）
_RUN_SEQ = itertools.count(1)


#: 出口三态 —— **和 `_Judgement.status` 同一个状态空间**，一一对应
OutcomeStatus = Literal["completed", "waiting_for_input", "incomplete"]


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


class _Judgement(BaseModel):
    """任务状态 —— **三态，不是两个布尔值**（用户给的模板）。

    ⚠️ **判定要吐的不只是"状态"，还要吐出"下一轮所需的信息"**——
    用户要求：*"没有完成的情况下，做到什么程度了，
    可以有一些有效信息支持下一轮 react 继续推进"*。
    否则推回去时它只能从头再来。
    """

    status: Literal["completed", "waiting_for_input", "incomplete"] = Field(
        description="任务状态"
    )
    #: 非 completed 时：**已经做到什么程度**（哪些部分已完成）
    #: —— 让下一轮能**接着推进**，而不是从头再来
    progress: str = Field(
        default="", description="status 不是 completed 时填：已经做到什么程度（哪些部分已完成）"
    )
    #: `incomplete` 时：**还缺什么** —— 平台据此**精确引导它回去继续**
    missing: list[str] = Field(
        default_factory=list,
        description="status=incomplete 时填：还缺什么（缺的产物 / 没做完的部分），逐条列出",
    )
    reason: str = Field(default="", description="一句话理由")


#: 判定规则 —— 用户拟的模板 + 用户补充的四点（有产物/无产物各自什么叫完成、
#: 未完成时报告进度、提问是**向主 agent 请求**而不是"向用户"）
_JUDGE_RULES = (
    "根据任务要求和实际执行结果，判断任务状态：\n"
    "- completed：任务已完成，且满足必要的交付条件。\n"
    "- waiting_for_input：任务尚未完成，且明确请求了继续所必需的信息。\n"
    "- incomplete：任务尚未完成，但不属于请求信息的情况。\n"
    "\n"
    "判定规则：\n"
    "1. **需要文件产物的任务**：检查产物是否存在、完整且符合要求。\n"
    "   产物可能有**多个**——逐个检查，缺一个都不算完成。\n"
    "   占位内容、待补充项、半成品（如「待补充」「TODO」「待填写」）**不能视为完成**。\n"
    "2. **不需要文件产物的任务**：确认实际工作已经完成。\n"
    "   不能仅凭执行者声称完成、或输出确认性文字（如「好的」「我了解了」「已收到」）判定。\n"
    "3. 只有**明确请求继续所必需的信息**，才能判定为 waiting_for_input。\n"
    "   解释失败原因、表达困惑、描述缺少的信息，**都不算**请求信息。\n"
    "\n"
    "另外，只要不是 completed，都要填写：\n"
    "- `progress`：**已经做到什么程度**（哪些部分已完成）——让下一轮能**接着推进**，"
    "而不是从头再来；\n"
    "- `missing`（仅 incomplete 时）：**还缺什么**，逐条列出，越具体越好。\n"
    "\n"
    "⚠️ **关于「平台截断」**：\n"
    "平台为了控制本提示词的体积，会把过长的产物**截断后**再交给你，并在中间插入\n"
    "形如「……【平台截断：中间 N 字……未展示给你】……」的标记。\n"
    "- 那是**平台加的**，不是作者写的占位符 —— **不要把它写进 `progress` / `missing`**；\n"
    "- **不要仅凭「看不到中间」就判 `incomplete`**（那会让产物越长越必然被判半成品）；\n"
    "- **但在你看得到的地方**（开头、结尾）发现「待补充」「TODO」「此处省略」这类\n"
    "  **作者写的**占位符，照常判未完成 —— 两者措辞不同：平台截断会带「平台截断」字样。\n"
)


def _nudge_text(j: _Judgement) -> str:
    """**推它继续**时说什么 —— 把判定吐出的 `progress` / `missing` 都喂回去。

    用户要求：*"尽可能暴露一些有用信息到外部，
    不然你只是说没完成，后续即使回去继续 react 也不知道该怎么做。"*
    """
    parts: list[str] = ["任务还没完成。"]
    if j.progress:
        parts.append(f"**你已经完成了**：{j.progress}")
    if j.missing:
        parts.append("**还缺**：\n" + "\n".join(f"- {x}" for x in j.missing))
    parts.append("请**接着往下做**，不要从头再来。")
    parts.append(
        "如果确实缺少必要信息才能继续，就**明确说明你需要什么**"
        "（缺哪个参数、要什么确认）；否则不要用「已完成」之类的话收尾。"
    )
    return "\n\n".join(parts)


def _clip(text: str, budget: int = JUDGE_ITEM_BUDGET) -> str:
    """**保头保尾**截断，中间**显式标注省略了多少字**。

    ⚠️ **标注省略这件事，比保头保尾本身更重要。**
    不标的话，判定**不知道自己没看全**，会基于不完整的信息**自信地下判断** ——
    那比"猜"更糟，是"以为自己没在猜"。

    保头保尾的理由：开头通常交代"这是什么/要做什么"，结尾是**最新状态**，中间多为过程。

    🔴 **标记的措辞是被实测逼出来的，别改回去。**
    原来写的是「……（此处省略 N 字）……」，判定会把它当成**作者偷懒的占位符**
    （`_JUDGE_RULES` 第 1 条：占位内容 / 待补充 / 半成品不能视为完成），
    于是**产物越长越必然判 `incomplete`** —— 而"产物长"恰恰是本架构要支持的正常场景。

    实测代价（`docs/discussion-log.md` F1）：
      - `s13`：一个任务被推回去 3 轮，**重写了 3 份重复教案**（607 秒 / 144k token），最终还记 `failed`
      - `e13`：单 run 从 **9.7k** token 涨到 **79k**，约 **8 倍**
      - 而且**不是确定性的** —— 同一轮里一个任务判 completed、另一个判 incomplete，看起来像"模型偶发不稳"
    """
    if len(text) <= budget:
        return text
    head = int(budget * _CLIP_HEAD_RATIO)
    tail = budget - head
    return (
        f"{text[:head]}\n"
        + _CLIP_MARK.format(n=len(text) - budget)
        + f"\n{text[-tail:]}"
    )


def _judge_material(arts: list[str], resp: dict) -> str:
    """判定要看的「实际执行结果」—— **两条路的取材不同，别合并**。

    | | 交付物是什么 | 执行器的散文是什么 |
    |---|---|---|
    | **有产物** | **产物正文**（就是它） | **过程话术**（含"需要调整吗？"这类礼貌收尾）—— **不是交付物** |
    | **无产物** | 只能从 AI 产出里找 → **全部轮次都要给** | **就是交付物本身** |

    🔴 **有产物时不要把散文喂进来。** 实测踩过：执行器在产物之后补了句
    "……如果需要进一步压缩，我马上帮你收紧。**需要调整吗？**"，
    判定看到后把它当成"明确请求继续所必需的信息" → 判成 `waiting_for_input`，
    而产物其实**已经完整交付**了。

    🔴 **无产物时不能只给最后一条。** 出口循环里消息是**累积**的
    （`messages = list(resp["messages"]) + [HumanMessage(nudge)]`），
    所以第 2 轮的 `resp` **包含第 1 轮的全部内容**。只取最后一条会让判定
    **看不到前面几轮的产出** —— 执行器说"我接着上面的进展"，而"上面"判定看不到。

    ⚠️ 后者是同一个 bug 的**第四次**（前三次见 `_judge` 的 docstring）。
    前三次都是"提示词忘了带某个字段"，这次是"信息在列表里、但取错了那一条" ——
    **前三次的检查法（"字段带了没有"）发现不了它**。

    每一项**各自**保头保尾（`_clip`），而不是整体截断 ——
    这样**每一轮都保证有代表**，不会被整体截断挤掉中间那轮。

    TODO（已记档，暂不做）：**`need_more` —— 有界的一次补看。**
      现在信息不够时判定只能硬判。可给 `_Judgement` 加一个 `need_more: str` 字段，
      判定点名要看哪一段 → 平台补上 → **再判一次**（只允许一次）。
      比给判定完整 tool loop 便宜得多，但仍是额外一轮 LLM 调用。
      **等本版上线后看实测**：判定还会不会因信息不够判错。会，再加。
    """
    parts: list[str] = []

    if arts:
        # 有产物 → 交付物就是产物正文，**不含散文**
        for aid in arts:
            if aid in ARTIFACTS:
                parts.append(f"【产物 {aid}】\n{_clip(ARTIFACTS[aid]['content'])}")
    else:
        # 无产物 → 交付物只能从 AI 产出里找，**全部轮次都要给**
        n = 0
        for m in resp["messages"]:
            if isinstance(m, AIMessage) and str(m.content).strip():
                n += 1
                parts.append(f"【产出 {n}】\n{_clip(str(m.content))}")

    if not parts:
        return "（执行者一句话都没说，也没产出任何文件）"

    # 总量兜底：超出就砍后面的项，并**显式说明少了几项**
    # ⚠️ 措辞同 `_CLIP_MARK` —— 也要防"被当成作者占位符"
    out, used = [], 0
    for p in parts:
        if used + len(p) > JUDGE_TOTAL_BUDGET and out:
            out.append(
                f"……【平台截断：另有 {len(parts) - len(out)} 项因提示词长度限制未展示给你，"
                "**你无法看到这些项**】……"
            )
            break
        out.append(p)
        used += len(p)
    return "\n\n".join(out)


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
    n = len(arts)
    art_line = f"执行者**产出了 {n} 个文件**。" if n else "执行者**没有产出任何文件**。"
    # 判定**输入**只记构成，不逐字复述 —— 里面既有产物正文、又有技能原文，两样都另有出处。
    trace.note(
        f"判定输入：技能 {skill.name}（instructions {len(skill.instructions)} 字，按名引用）"
        f"｜已知信息 {len(briefing.strip())} 字（委派时给的）"
        f"｜材料 "
        + (
            "、".join(f"{a}（{len(ARTIFACTS[a]['content'])} 字）" for a in arts if a in ARTIFACTS)
            or "执行器散文（无产物）"
        )
        + f"（材料共 {len(material)} 字）"
    )
    prompt = (
        f"【任务要求】\n{skill.instructions}\n\n"
        f"【执行者已经拿到的信息】\n{briefing.strip() or '（无）'}\n\n"
        f"【实际执行结果】\n---\n{material}\n---\n\n"
        f"【产物情况】{art_line}\n\n"
        f"{_JUDGE_RULES}"
    )
    return safe_structured(
        _Judgement,
        prompt,
        # 两次都失败时保守当作「没完成、也不问用户」→ 会让它再 react 一轮
        _Judgement(status="incomplete", reason="判定调用失败，保守处理"),
        tag="run_task → 判定任务状态",
        # 🔴 判定 prompt 里嵌着**产物正文**（`_judge_material` 喂的）和**技能原文**
        # （【任务要求】那段）—— 两样都另有出处（产物落盘 / `evals/skills/`），
        # 日志只记上一行"判定输入"的构成，不逐字复述（用户要求）。
        log_prompt=False,
    )


def run_task(skill: Skill, briefing: str = "") -> ExecOutcome:
    """通用执行器：干活、**判定出口**、没出口就推它继续。

    Args:
        skill: 专业能力包（指令从这里来）。
        briefing: 这次交给它的全部已知 —— 由 `agent.render_briefing(task)` 渲染。
            平台**不解析、不抽取、不比对**，原样带给执行器。
            ⚠️ 名字是 briefing 不是 given：它是**整份交接**（任务 / 上次做到哪 / 历次答复 /
            已产出产物 / 可读产物），不是"这一轮的增量"—— 执行器用完即弃，每次都是重入。
            ⚠️ **可读产物清单也由它承载**（原来是 `run_task` 单独拼一个 `artifacts` 参数，
            两处都渲染 → 执行器看到两遍；现在只有一个来源）。

    Returns:
        `ExecOutcome` —— 平台据此定任务状态（**不再靠"产物库空不空"猜**）。
    """
    agent = create_agent(
        get_model(),
        tools=[write_artifact, read_artifact, read_skill_file],
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
        refs = (
            "\n\n【技能自带文件】要用哪一份就用 `read_skill_file` 读全文"
            "（路径照抄，**不要凭文件名猜内容**）：\n" + "\n".join(have)
        )
    task_text = f"【能力指令】\n{skill.instructions}{refs}\n\n【已知信息】\n{briefing_text}"

    run_id = f"R{next(_RUN_SEQ):02d}"
    trace.section(f"run_task · {run_id} · skill={skill.name}（通用执行器，隔离上下文）")
    # 🔴 **能力指令按名引用，不逐字复述**（用户要求：读了 skill 只写"读了啥、谁读的"）——
    # 技能原文是静态资产、另有落盘文件；日志里真正要留的是**这次的交接内容**。
    trace.block(
        "发给执行器的内容（能力指令按名引用）",
        f"【能力指令】{skill.name}"
        f"（instructions {len(skill.instructions)} 字，原文见该技能包的 SKILL.md）\n\n"
        f"【已知信息】\n{briefing_text}",
    )
    t0 = time.time()

    messages: list = [HumanMessage(task_text)]
    outcome = ExecOutcome()
    #: 进来之前就存在的产物 —— 这次跑**读到的**不算它交付的（见 `_recover_artifacts`）
    preexisting = set(ARTIFACTS)
    for round_i in range(MAX_REACT_ROUNDS):
        # 不传 config —— 传了执行器会变成独立根图
        resp = agent.invoke({"messages": messages})
        trace.dump_trace(f"run_task 第 {round_i + 1} 轮", resp["messages"])

        outcome.artifacts = _recover_artifacts(resp, preexisting)
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
        trace.note(
            f"{run_id} 第 {round_i + 1} 轮判定：status={j.status}　·　"
            f"判定看到：{material.count('【产出')} 段产出 + {len(outcome.artifacts)} 个产物"
            f"（{len(material)} 字）\n"
            f"          理由：{j.reason[:160]}\n"
            f"          进度：{j.progress[:160] or '（无）'}\n"
            f"          还缺：{j.missing or '（无）'}"
        )

        if j.status in ("completed", "waiting_for_input"):
            break
        # 剩下只有 incomplete —— **穷举完了，没有第四种**

        # 🔴 **incomplete → 不让它停**，推回去继续。
        #    这才是"出口限制"的意义：执行器没干完活是**它的问题**，不该转嫁给用户。
        #    判定给了 `missing` 就**精确告诉它缺什么**，比笼统一句"你还没做完"有效。
        if round_i + 1 < MAX_REACT_ROUNDS:
            nudge = _nudge_text(j)
            trace.note(f"⚠️ 第 {round_i + 1} 轮 incomplete → 推它继续（提示词见下）")
            trace.block("推回去的提示词", nudge)
            messages = list(resp["messages"]) + [HumanMessage(nudge)]
        else:
            # **显式写出这个分支**：推满了，不再推 → outcome 保持 incomplete → delegate 判 failed
            trace.note(f"⚠️ 第 {round_i + 1} 轮 incomplete 且已到轮次上限 → 不再推，交给主 agent")

    trace.note(
        f"run_task 结束 · {run_id} · skill={skill.name}：status={outcome.status} "
        f"轮次={outcome.rounds} 产物={outcome.artifacts or '（无）'}"
        f"{'　（多轮）' if outcome.rounds > 1 else ''}"
        f"（{time.time() - t0:.1f}s）"
    )
    return outcome


def _recover_artifacts(resp: dict, preexisting: set[str] | None = None) -> list[str]:
    """从工具结果里捞产物 ID（只认产物库里真实存在的），**且必须是这次新写的**。

    🔴 `preexisting` 不是可选的优化，是正确性：执行器 `read_artifact` 读前序产物时，
    那个产物 ID 会出现在它的消息里 —— 不排除的话，**读来的会被算成它交付的**。
    实测（2026-10-29，`t2-refs2`）：guided-learning 读了 `art_001`，任务记录就变成
    「产出 `art_001`、`art_002`」，主 agent 被告知"已交付 art_001, art_002"，
    判定取材也把教案正文一并喂了进去。

    ⚠️ 已知窗口：两个执行器**并行**时，各自算差集可能把兄弟刚写的产物算进来。
    与 `store_artifact` 的 ID 竞态同源、同量级（难触发），先记档不修。
    """
    found: list[str] = []
    for m in resp["messages"]:
        text = str(m.content)
        for aid in ARTIFACTS:
            if aid in text and aid not in found and aid not in (preexisting or ()):
                found.append(aid)
    return found


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
