"""Skill = 专业能力包（静态）。

**只有两样东西**（对应「Skill 是专业能力」）：
  - `description`：这个能力是干什么的（主 agent 靠它判断"什么时候该用"）
  - `instructions`：完整工作流程（派发时交给执行器，**判定完成时也带进提示词**）

**平台对 skill 作者零强制约束**——不加任何字段：
  - ❌ 不要求声明**参数**（`params`）—— 参数名是平台的词汇表，用户的自然语言不该被塞进去；
    而且实测模型会改写参数名（「学科」→「科目」），靠它去重本来就不可靠。
    **正解**：作者在 `instructions` 里用自然语言写「你需要知道 X」（B2）。
  - ❌ 不要求声明**交付形态 / 结果协议**（`deliverables` / `finish` / `status`）。
    **正解**：判定用三态（问"任务完成了没"），产物只是证据之一（D1）。
  - ❌ 不要求声明**完成条件**（`completion_criteria`）—— 实测它反而更差（4/6 vs 6/6），
    因为要求本来就在 `instructions` 里，再抄一遍是重复。

产物库是"文件系统"的替身：正文写进去，只把 ID 交回主 agent（**上下文隔离的落点**）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from langchain.tools import tool

# ──────────────────────────── 产物库（= 文件系统替身） ────────────────────────────

ARTIFACTS: dict[str, dict[str, str]] = {}


def store_artifact(title: str, summary: str, content: str) -> str:
    """把产物落进产物库，返回产物 ID。"""
    aid = f"art_{len(ARTIFACTS) + 1:03d}"
    ARTIFACTS[aid] = {"title": title, "summary": summary, "content": content}
    return aid


@tool
def write_artifact(title: str, summary: str, content: str) -> str:
    """把成品写入产物库。正文不会再回到主 agent 的上下文，只返回产物 ID。

    Args:
        title: 产物名。
        summary: **一句话梗概** —— 它是产物对外露出的那一层：主 agent 靠它决定
            要不要读全文、要不要把它传给下一个技能。不写梗概，别人只能读全文。
        content: 产物正文全文。
    """
    aid = store_artifact(title, summary, content)
    return f"已写入产物 {aid}（{len(content)} 字）"


@tool
def read_artifact(artifact_id: str) -> str:
    """按需读回产物全文（渐进式披露第二层）。

    ⚠️ **读了就进你的上下文**，长产物会一直占着。只有确实要看内容时才读；
    只是把它交给下一个任务，用 `delegate(..., artifacts=[...])` 传 ID 就够了。
    """
    a = ARTIFACTS.get(artifact_id)
    return f"【{a['title']}】\n{a['content']}" if a else f"产物 {artifact_id} 不存在"


def artifact_index_lines() -> list[str]:
    """产物清单（渐进式披露第一层）：ID / 名称 / 字数 / 梗概，**不含正文**。"""
    return [
        f"- {aid}《{a['title']}》（{len(a['content'])} 字）—— {a.get('summary') or '（无梗概）'}"
        for aid, a in ARTIFACTS.items()
    ]


@tool
def list_artifacts() -> str:
    """列出本会话已交付的产物：ID、名称、字数、梗概。**不含正文。**

    要判断"该把哪份产物交给下一个任务"时用它；正文按需用 `read_artifact` 读。
    """
    if not ARTIFACTS:
        return "本会话还没有产物。"
    return "本会话已交付的产物：\n" + "\n".join(artifact_index_lines())


@tool
def read_skill(skill_name: str) -> str:
    """按需读一个能力的**完整指令**（渐进式披露第二层）。

    常驻在你眼前的只有能力名和一句话描述。写任务要求前若不确定这个能力
    **交付什么、按什么标准交付**，用它读原文核对 —— **不要凭描述猜**。
    """
    # 延迟导入：`agent` 反向依赖本模块，模块级导入会成环；
    # 且技能表以 `proto.agent.SKILLS` 为准（delegate 读的就是它，评测会替换它）。
    from proto.agent import SKILLS

    skill = SKILLS.get(skill_name)
    if skill is None:
        return f"没有能力「{skill_name}」。可用：{'、'.join(SKILLS)}"
    return f"【能力 {skill.name}】{skill.description}\n\n{skill.instructions}"


# ──────────────────────────── Skill 定义 ────────────────────────────


@dataclass(frozen=True)
class Skill:
    """专业能力包（静态、可版本化）。

    **只有两样东西**：这个能力是干什么的（description）+ 怎么干（instructions）。

    ⚠️ 两个被删掉的字段，记在这里免得再走回头路：

    **`params`（参数声明）—— 已删**
      - 它逼着作者声明参数，而"参数名"是平台的词汇表，用户的自然语言不该被塞进去
      - 实测模型会改写参数名（「学科」→「科目」），靠它做去重本来就不可靠

    **`completion_criteria`（完成条件）—— 已删**（2026-10-28 实测后删）
      - 它**本质也是 LLM 判断**（"逐条核对"还是 LLM 在做），不是确定性机制
      - 对比过五种提示词（`experiments/debug_judge_variants.py`，6 个用例）。
        ⚠️ **单次采样的分数会漂**：2026-10-28 重跑 A/B/C/E **全是 5/6**，
        只有 **F（用户模板）6/6** —— 早先记的 "C 4/6""E 6/6" 已不可复现。
        另注：该实验**有混淆**（A–E 用两个布尔 schema，F 用三态枚举）。
      - **criteria 没有增量价值**——要求本来就在 `instructions` 里，再抄一遍是重复。
        砍它的理由是"没必要"，**不是"分数低"**（`debug_criteria.py` 自己写的测试
        **9/9 全过**仍然不划算）
      - 而且它**会误导**：把"必须包含 X"硬套到不该套的任务上
      - **正解是把 `instructions` 带进判定提示词 + 让模型先反省再判断**
    """

    name: str
    #: 主 agent 靠它判断"什么时候该用这个能力"
    description: str
    #: 完整工作流程 —— 派发时原样交给执行器，**判定完成时也带进提示词**
    instructions: str


SKILLS: dict[str, Skill] = {
    "lesson-prep": Skill(
        name="lesson-prep",
        description="备课：产出教学设计要点（目标 / 重难点 / 教学过程提纲）",
        instructions=(
            "产出教学设计的要点：教学目标、重难点、教学过程提纲。精简，150 字以内。\n"
            "**你需要知道这节课的「课题」和「课时」（第几课时）**"
            "——不知道就先问，不要自己编，也不要用默认值糊过去。"
        ),
    ),
    "guided-learning": Skill(
        name="guided-learning",
        description="导学单：产出学生用导学单要点（学习目标 / 预习任务 / 当堂检测）",
        instructions=(
            "产出导学单要点：学习目标、预习任务、当堂检测。精简，150 字以内。\n"
            "**你需要知道「课时」（第几课时）**——不知道就先问。"
        ),
    ),
    "quiz": Skill(
        name="quiz",
        description="随堂小测：产出 3 道随堂小题（含答案）",
        instructions=(
            "产出 3 道随堂小题（含答案）。精简，150 字以内。\n"
            "**你需要知道「难度」**（基础 / 提升 / 挑战 三档）——不知道就先问。"
        ),
    ),
    # 交付形态 = **正文**：成品是给**调用方解析**的协议块，**不落库**。
    # 用来验 S7：产物库为空时，"完成 = 产物库非空" 这条判定会不会误伤它。
    "courseware-config": Skill(
        name="courseware-config",
        description="课件配置生成：产出给调用方解析的配置块（不落库，走正文）",
        instructions=(
            "产出一段给调用方解析的课件配置块，格式必须严格遵守：\n"
            "```courseware-config\n"
            '{"grade": "年级", "subject": "学科", "topic": "课题", "slides": 6}\n'
            "```\n"
            "**这段配置块直接写在你的回复正文里就行——它是给调用方解析的，"
            "不需要写入产物库。**"
        ),
    ),
}


def catalog() -> str:
    """给规划用的能力清单（只有名字与描述 —— 渐进式披露的第一层）。"""
    return "\n".join(f"- {s.name}：{s.description}" for s in SKILLS.values())
