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

import itertools
import re
from dataclasses import dataclass, field
from pathlib import Path

from langchain.tools import tool

# ──────────────────────────── 产物类型（声明 + 硬性派发） ────────────────────────────

#: **声明的文件类型 → 扩展名** —— 平台据此**硬性派发**保存：说 md 就存 `.md`，
#: 说 html 就存 `.html`。**不嗅探内容**（2026-10-30 用户纠正：
#: *"产物是什么 是生成产物的时候 ai 说的 跟什么梗概 title 一起产出的
#: 后续对产物的保存只是根据文件类型 硬性派发处理 你不要多余操作"*）。
#:
#: 要支持新类型就往这张表里加一行 —— 派发规则只有这一处。
FILE_TYPES: dict[str, str] = {
    "md": ".md",
    "markdown": ".md",
    "html": ".html",
    "htm": ".html",
}
#: 声明缺失 / 认不出来时存成什么。**不报错**：产物已经产出来了，存不下比存错更糟。
DEFAULT_EXT = ".md"


def file_ext(file_type: str) -> str:
    """声明的类型 → 扩展名（大小写、带不带点、`markdown`/`htm` 都认）。"""
    return FILE_TYPES.get((file_type or "").strip().lstrip(".").lower(), DEFAULT_EXT)


def artifact_slug(s: str, n: int = 40) -> str:
    """标题 → 文件名安全的一段（保留中英文，其余压成 `-`）。"""
    return (re.sub(r"[^\w一-鿿]+", "-", s).strip("-") or "untitled")[:n]


# ──────────────────────────── 产物库（= 文件系统替身） ────────────────────────────

ARTIFACTS: dict[str, dict[str, str]] = {}

#: 产物 ID 的**发号器** —— 用 `itertools.count()` 而不是 `len(ARTIFACTS) + 1`。
#:
#: 🔴 **`len(ARTIFACTS)+1` 是"先查后写"，两步之间有窗口**：两个执行器同时算，
#: 会拿到**同一个 ID**，后写覆盖先写 —— 产物**静默丢失**。
#: `evals/unit_artifact_race.py` 专门压这个：窗口在微秒级，端到端跑不到
#: （3 个并行 delegate 的写入间隔 4–205 秒），只能靠单元测试用 Barrier + 拉满
#: GIL 切换频率放大出来。
#:
#: 2026-10-30 实况：给 `store_artifact` 加"当场落盘"时，**两步之间多插了活
#: （嗅探类型、拼文件名），窗口被撑开** —— 单测当场从 🟡"没撞上"翻成
#: 🔴"撞了 18577 次"。`next()` 在 GIL 下是原子的，换掉就闭合了。
#: （ID 因此不再等于"库里第几个"—— 没有任何地方依赖这个。）
_ART_SEQ = itertools.count(1)

#: 产物**落盘到哪** —— 由入口设（`evals.chat` 设成本次 run 的产物目录）。
#: 不设 = 只进内存，最后由 `dump_artifacts` 统一落盘（跑批那条路径就是这样）。
ARTIFACT_DIR: Path | None = None


def artifact_body(aid: str, a: dict) -> str:
    """落盘时写什么 —— HTML **原样**（前面加 markdown 头的话，浏览器会把标题当正文渲染），
    其余加一个来源头（人工翻文件时知道这是哪一份）。"""
    if a["ext"] == ".html":
        return a["content"]
    return f"# {a['title']}\n\n> `{aid}`｜{len(a['content'])} 字\n\n---\n\n{a['content']}\n"


def store_artifact(title: str, summary: str, file_type: str, content: str) -> str:
    """把产物落进产物库，返回产物 ID。**顺手落盘**（设了 `ARTIFACT_DIR` 的话）。

    🔴 **为什么当场落盘**（2026-10-30 用户提的）：产物是**交付物**，用户当场就要能打开。
    以前只在跑完时统一落盘，于是 `write_artifact` 那句"已写入产物 art_001"
    **指不到任何文件** —— 用户原话：*"保存地址要说一下 不然不清楚哪里找去"*。

    记录里带 `type`（声明的类型）/ `ext`（扩展名）/ `path`（真实路径）——
    **产物不只是正文**，后面还要有代码按类型处理它（md 归 md、html 归 html）。
    """
    aid = f"art_{next(_ART_SEQ):03d}"
    ext = file_ext(file_type)
    rec: dict[str, str] = {
        "title": title,
        "summary": summary,
        "content": content,
        "type": file_type,
        "ext": ext,
        "file": f"{aid}_{artifact_slug(title)}{ext}",
    }
    if ARTIFACT_DIR is not None:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        p = ARTIFACT_DIR / rec["file"]
        p.write_text(artifact_body(aid, rec), encoding="utf-8")
        rec["path"] = str(p)
    ARTIFACTS[aid] = rec
    return aid


@tool
def write_artifact(title: str, summary: str, file_type: str, content: str) -> str:
    """把成品写入产物库。正文不会再回到主 agent 的上下文，只返回产物 ID。

    Args:
        title: 产物名。
        summary: **一句话梗概** —— 它是产物对外露出的那一层：主 agent 靠它决定
            要不要读全文、要不要把它传给下一个技能。不写梗概，别人只能读全文。
        file_type: **文件类型**（`md` / `html`）—— 由**产出方**在交付时一起说，
            平台据此**硬性派发**保存：说 md 就存 `.md`，说 html 就存 `.html`。
        content: 产物正文全文。
    """
    aid = store_artifact(title, summary, file_type, content)
    a = ARTIFACTS[aid]
    # 返回里**带上文件类型和保存地址** —— 用户和后面的代码都要靠它找到这份产物
    where = f"\n文件：{a['path']}" if a.get("path") else ""
    return f"已写入产物 {aid}（{len(content)} 字，{a['ext'].lstrip('.')}）{where}"


@tool
def read_artifact(artifact_id: str) -> str:
    """按需读回产物全文（渐进式披露第二层）。

    ⚠️ **读了就进你的上下文**，长产物会一直占着。只有确实要看内容时才读；
    只是把它交给下一个任务，用 `delegate(..., artifacts=[...])` 传 ID 就够了。
    """
    a = ARTIFACTS.get(artifact_id)
    return f"【{a['title']}】\n{a['content']}" if a else f"产物 {artifact_id} 不存在"


def artifact_index_lines() -> list[str]:
    """产物清单（渐进式披露第一层）：ID / 名称 / 字数 / **类型** / 梗概，**不含正文**。"""
    return [
        f"- {aid}《{a['title']}》（{len(a['content'])} 字，{a.get('ext', '.md').lstrip('.')}）"
        f"—— {a.get('summary') or '（无梗概）'}"
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
