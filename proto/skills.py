"""**Skill** = 专业能力包（静态）。

**只有两样东西**（对应「Skill 是专业能力」）：
  - `description`：这个能力是干什么的（主 agent 靠它判断"什么时候该用"）
  - `instructions`：完整工作流程（派发时交给执行器，**判定完成时也带进提示词**）

🔴 **技能包里的参考文件不是这里的字段**：它本来就在磁盘上读得到，
装进对象里是多余的一份；读不读、读哪份是 LLM 自己的决策。
列和读在 `proto/skill_pack.py`（`list_skill_files` / `read_skill_file`）。

**平台对 skill 作者零强制约束**——不加任何字段：
  - ❌ 不要求声明**参数**（`params`）—— 参数名是平台的词汇表，用户的自然语言不该被塞进去；
    而且实测模型会改写参数名（「学科」→「科目」），靠它去重本来就不可靠。
    **正解**：作者在 `instructions` 里用自然语言写「你需要知道 X」（B2）。
  - ❌ 不要求声明**交付形态 / 结果协议**（`deliverables` / `finish` / `status`）。
    **正解**：判定用三态（问"任务完成了没"），产物只是证据之一（D1）。
  - ❌ 不要求声明**完成条件**（`completion_criteria`）—— 实测它反而更差（4/6 vs 6/6），
    因为要求本来就在 `instructions` 里，再抄一遍是重复。

🔴 **和 `artifacts.py` 是两个领域**（2026-10-30 分开）：这里只管**静态能力包**；
运行期的交付物在 `artifacts.py`。**技能包怎么从磁盘读**在 `skill_pack.py`（I/O + 格式）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from langchain.tools import tool


# ──────────────────────────── Skill 定义 ────────────────────────────


@dataclass(frozen=True)
class Skill:
    """专业能力包（静态、可版本化）。

    **三样东西**：这个能力是干什么的（description）、怎么干（instructions）、
    以及**可选的**参考资料（references）。

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


@tool
def read_skill(skill_name: str) -> str:
    """按需读一个能力的**完整指令**（渐进式披露第二层），附带它自带哪些文件。

    常驻在你眼前的只有能力名和一句话描述。写任务要求前若不确定这个能力
    **交付什么、按什么标准交付**，用它读原文核对 —— **不要凭描述猜**。
    """
    # 延迟导入：`agent` 反向依赖本模块，模块级导入会成环；
    # 且技能表以 `proto.agent.SKILLS` 为准（delegate 读的就是它，评测会替换它）。
    from proto.agent import SKILLS

    skill = SKILLS.get(skill_name)
    if skill is None:
        return f"没有能力「{skill_name}」。可用：{'、'.join(SKILLS)}"
    out = f"【能力 {skill.name}】{skill.description}\n\n{skill.instructions}"
    # 参考文件清单**现场扫盘**（不缓存在 Skill 上，见 `skill_pack` 的模块 docstring）。
    # 有才带这一段 —— 没有就一个字不加（**没有就是没有**，不写"（无参考文件）"这种废话）。
    from proto.skill_pack import skill_file_index_lines

    files = skill_file_index_lines(skill.name)
    if files:
        out += (
            "\n\n【技能自带文件】要用哪一份就用 `read_skill_file` 读全文"
            "（路径照抄，**不要凭文件名猜内容**）：\n" + "\n".join(files)
        )
    return out
