"""**Skill** = 专业技能包（静态）。

## 这个文件是什么

只有两样东西：`Skill` 这个数据类，和**内置技能表**（import 时登记进 `proto.registry`
—— 表的唯一持有者是注册表，**本模块不持有表**）。
**零磁盘 I/O、零工具**：技能包怎么从磁盘读、两个读工具（`read_skill` /
`read_skill_file`）都在 `proto/skill_pack.py`；运行期的交付物在 `proto/artifacts.py`。

## 设计原则（不变量）

🔴 **技能包里的参考文件不是这里的字段**：它本来就在磁盘上读得到，
装进对象里是多余的一份；读不读、读哪份是 LLM 自己的决策。〔沿革 3〕

**平台对 skill 作者零强制约束**——不加任何字段：

- ❌ 不要求声明**参数**（`params`）—— 参数名是平台的词汇表，用户的自然语言不该被塞进去；
  而且实测模型会改写参数名（「学科」→「科目」），靠它去重本来就不可靠。
  **正解**：作者在 `instructions` 里用自然语言写「你需要知道 X」（B2）。〔沿革 1〕
- ❌ 不要求声明**交付形态 / 结果协议**（`deliverables` / `finish` / `status`）。
  **正解**：出口三态由**结构**算（申报要信息 / 交了产物 / 都没有），产物只是证据之一（D1）。
- ❌ 不要求声明**完成条件**（`completion_criteria`）—— 它**本质也是 LLM 判断**，
  不是确定性机制；而且它**会误导**：把"必须包含 X"硬套到不该套的任务上。
  **正解**：平台**不判"够不够"** —— 只报结构事实；**判据以技能自己声明的交付标准为准**
  （主 agent 拿回执去核）。〔沿革 2〕

## 沿革（改过什么、为什么改）

> 编号是**引用锚**（正文里写 `〔沿革 N〕`）—— **只追加、不改号**，新条目加在**末尾**。

1. **`params`（参数声明）没加**。它逼着作者声明参数，而"参数名"是平台的词汇表，
   用户的自然语言不该被塞进去；实测模型会改写参数名（「学科」→「科目」），
   靠它做去重本来就不可靠。正解是作者在 `instructions` 里自然语言写「你需要知道 X」。
2. **`completion_criteria`（完成条件）删掉**（2026-10-28 实测后删）。它**本质也是
   LLM 判断**（"逐条核对"还是 LLM 在做），不是确定性机制。对比过五种提示词
   （`experiments/debug_judge_variants.py`，6 个用例）—— ⚠️ **单次采样的分数会漂**：
   2026-10-28 重跑 A/B/C/E **全是 5/6**，只有 **F（用户模板）6/6**；早先记的
   "C 4/6""E 6/6" 已不可复现。另注：该实验**有混淆**（A–E 用两个布尔 schema，
   F 用三态枚举）。砍它的理由是"没必要"，**不是"分数低"** —— `debug_criteria.py`
   自己写的测试 **9/9 全过**仍然不划算。
3. **和 `artifacts.py` 是两个领域**（2026-10-30 分开）：这里只管**静态技能包**；
   运行期的交付物在 `artifacts.py`。
4. **`catalog()` 转发壳删**（2026-11-03 审核）：它只是 `return registry.catalog()` ——
   零调用点，而且"技能清单只拼一次"的目标下，多一个同名出口就是多一个将来被抄错的地方。
   要清单请直接 `registry.catalog()`。
5. **两个读工具搬进 `skill_pack`**（2026-11-06 搬完，外审 F2/F3）：`read_skill` 原来住这里，
   它要磁盘 I/O，于是**函数内 lazy import** `proto.skill_pack` —— 一个被藏起来的环。
   搬去和 `read_skill_file` 作伴之后，本模块**没有任何 skill_pack 引用**
   （零磁盘 I/O、零工具，只剩纯领域对象 ＋ 内置表登记）。
   守卫：`evals/unit_registry.py` ① 的"proto 运行时 import 图无环"。
"""

from __future__ import annotations

from dataclasses import dataclass

from proto.registry import registry


# ──────────────────────────── Skill 定义 ────────────────────────────


@dataclass(frozen=True)
class Skill:
    """专业技能包（静态、可版本化）。

    **三个字段**：叫什么（`name`）、干什么（`description`）、怎么干（`instructions`）。
    参考文件**不在对象里** —— 它们留在磁盘上，读不读、读哪份是 LLM 自己的决策，
    列与读都在 `skill_pack.py`。

    ⚠️ **两个被删掉的字段，记在这里免得再走回头路**：`params`（参数声明）、
    `completion_criteria`（完成条件）—— 为什么不加，见〔沿革 1〕〔沿革 2〕。
    """

    name: str
    #: 主 agent 靠它判断"什么时候该用这个技能"
    description: str
    #: 完整工作流程 —— 派发时原样交给执行器，**判定完成时也带进提示词**
    instructions: str


#: 内置技能表 —— **登记进注册表，不再是一个模块级变量**（R1）。
#: 唯一持有者是 `proto.registry`；这里只是"内置的那一份"的声明。
#: ⚠️ 不要写 `from proto.skills import SKILLS` —— 那会重新制造一个绑定点（见 registry 模块 docstring）。
registry.register_defaults({
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
})
