"""**技能包** —— 文件夹形态的技能，以及它的参考文件（I/O + 格式）。

一个技能 = 一个文件夹：

    lesson-prep/
    ├── SKILL.md               ← 入口，**只有这一个名字**；frontmatter 给 name/description，正文即 instructions
    ├── references/            ← **可选**，没有就没有（不是错误）
    │   ├── 教学目标编写指南.md
    │   └── 教案结构模板.md
    └── assets/                ← 目录名随作者，**整个包都能读**
        └── 教案汇总模板.md

## 🔴 技能包里的文件**不进 `Skill` 对象**

它本来就在磁盘上读得到 —— 装进对象里是**多余的一份**。
`Skill` 因此仍然只有 `name` + `description` + `instructions`（用户 2026-10-30 定的，
也是 §0.6 C2「平台尽量少约束 skill 作者」和评审 D1「不引入任何新字段」的延续）。

**读不读、什么时候读、读哪一份，是 LLM 自己的决策** —— 这里只提供两件事：
`list_skill_files()`（有哪些）和 `read_skill_file()`（读一份）。两者**都现场扫盘**，
内存里不留副本。

🔴 **两个 LLM 读工具也住这里**（2026-11-05，F2/F3）：`read_skill`（技能原文 ＋
自带文件清单）和 `read_skill_file`（读一份文件）—— 它们同时要"领域对象"
（`registry`）和"磁盘 I/O"（本模块），住在 `skills.py` 就得函数内 lazy import
本模块（**一提就成环**，探针 `tmp/probe_cycle_skills_pack.py` 实测）。提示块本身
（`skill_file_prompt`）也是**唯一一份** —— `executor.run_task` 的任务书用它。

## 🔴 **不限定子目录名**

作者的资料放 `references/` 还是 `assets/` 还是别的，**是作者的事** ——
平台规定"参考资料必须叫 references"就是用目录名约束作者（跟 C2 相冲）。
实测 `pbl-teaching-designer` 就把阶段模板放在 `assets/`（用户 2026-10-30 定：整个包都能读）。

## 只认 `SKILL.md`

入口文件名**只有一种**。不做"跟目录同名"的兼容 ——
两种写法并存会让"这个目录到底是不是一个技能包"变得要靠猜。

## 收与不收

**收**：扩展名在白名单里的文本文件（`TEXT_EXTS`），含子目录（名字带相对路径）。
**不收**：二进制、超大文件、隐藏目录 —— **在"列"这一步就过滤掉**，
所以 LLM 的清单里根本不会出现读不了的东西。

> 🔑 **一条不变量**：`read_skill_file` **只认 `list_skill_files` 给得出来的东西**。
> 路径穿越（`../`）、二进制、超大文件，全被这一条挡住 —— 不需要再写一套路径检查。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from langchain.tools import tool

from proto.registry import registry
from proto.skills import Skill

#: 技能包的入口文件名 —— **只有这一个**。
ENTRY_NAME = "SKILL.md"

#: 参考文件目录名。
REFERENCES_DIR = "references"

#: 收哪些扩展名当参考文件（小写、含点）。**要加就加这里。**
TEXT_EXTS: frozenset[str] = frozenset(
    {".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ".csv", ".tsv",
     ".html", ".xml", ".toml", ".ini", ".sql", ".log", ".py", ".js", ".ts"}
)

#: 单个参考文件的大小上限（字节）。超了就**不列出来** ——
#: 参考文件是给 LLM 读的，几 MB 的那种多半是放错了地方，不是我们该默默吞下的。
MAX_REF_BYTES = 256 * 1024

#: 技能名 → 它的**包目录**。加载时记下，读参考文件时按它去找。
#:
#: ⚠️ 这里存的是**位置**，不是内容 —— 参考文件始终留在磁盘上，读的时候才取。
#: 所以它不是 `Skill` 的字段（`Skill` 只管"这个技能是什么"，不管"它在哪个盘上"）。
PACK_DIRS: dict[str, Path] = {}


def split_frontmatter(text: str) -> tuple[dict, str]:
    """拆 `---` 包裹的 YAML frontmatter 与正文；没有 frontmatter 时元数据为空。

    🔴 **结束标记必须是"独占一行"的 `---`** —— 不能简单 `text.split("---")`：
    实测真实技能的 frontmatter 里有 `artifact_start: "---教案开始---"`，
    **值里就带 `---`**，naive split 会在引号中间断开、YAML 直接解析失败
    （2026-10-30 拿真实技能包一试就撞上）。**按行找分隔符**才稳。
    """
    s = text.lstrip("﻿").lstrip()  # BOM 也算 —— 有的编辑器会写进去
    lines = s.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            try:
                meta = yaml.safe_load("\n".join(lines[1:i])) or {}
            except yaml.YAMLError:
                return {}, text
            body = "\n".join(lines[i + 1 :]).lstrip("\n")
            return (meta if isinstance(meta, dict) else {}), body
    return {}, text


# ──────────────────────────── 技能包里的文件：列 + 读 ────────────────────────────


def list_skill_files(skill_name: str) -> list[tuple[str, int]]:
    """技能包里的文本文件 → `[(相对包根的路径, 字数), ...]`。**现场扫盘，不缓存。**

    🔴 **不限定子目录名**（2026-10-30 用户定）：作者爱放 `references/` 还是 `assets/`
    还是别的，是**作者的事** —— 平台去规定"参考资料必须叫 references"，
    等于用目录名约束作者（跟 §0.6 C2「平台尽量少约束 skill 作者」相冲）。
    实测就有技能把阶段模板放在 `assets/`。

    **排除**：入口 `SKILL.md` 自己（它的正文已经是 `instructions` 了）、
    隐藏目录（`.git` 之类）、非文本扩展名、超大文件。

    没有技能包信息 / 目录里没东西 —— 一律返回**空列表**：**没有就是没有，不是错误**。
    """
    pack = PACK_DIRS.get(skill_name)
    if pack is None:
        return []
    out: list[tuple[str, int]] = []
    for p in sorted(pack.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(pack)
        if rel.as_posix() == ENTRY_NAME or any(part.startswith(".") for part in rel.parts):
            continue
        if p.suffix.lower() not in TEXT_EXTS or p.stat().st_size > MAX_REF_BYTES:
            continue
        # 🔴 名字用**相对技能包根**的路径（`references/stage-1-topic.md`），
        # **和 SKILL.md 里写的一模一样** —— 作者本来就是那么引用的
        # （实测：`[名](references/xxx.md)`、`见 references/xxx.md`）。
        # 用"相对 references/"会让模型照抄 SKILL.md 时读不到（2026-10-30 撞上）。
        out.append((rel.as_posix(), len(p.read_text(encoding="utf-8", errors="replace"))))
    return out


def skill_file_index_lines(skill_name: str) -> list[str]:
    """技能包文件清单（渐进式披露第二层半）：路径 + 字数，**不含正文**。"""
    return [f"- {name}（{n} 字）" for name, n in list_skill_files(skill_name)]


def skill_file_prompt(skill_name: str) -> str:
    """【技能自带文件】提示块（清单常驻、正文按需）—— **唯一一份**；没文件返回空串。

    🔴 **为什么抽成函数**（2026-11-05，F2）：这段提示原来在**两处各拼一遍、逐字重复**
    —— `read_skill` 的工具返回（主 agent 侧）和 `executor.run_task` 的任务书（执行器侧）。
    改一处忘另一处，同一个东西就是两副面孔（"一套，别分叉"；实测 diff 只差变量名）。

    🔴 **必须把 `skill_name` 写进清单**（2026-11-02 实测踩到）：清单里只有路径，
    而 `read_skill_file(skill_name, path)` 的**第一个参数是技能名** —— 不写，
    执行器就只能**猜**：实测它试了「课堂活动配置生成」「classroom-activity-config」
    「activity-config」「config」四个变体，全返回"没有自带文件"，卡了 121 秒后放弃，
    反过来问用户"这个技能的确切名称是什么"。
    ⚠️ 这个坑只在"**技能包有 references 且活是委派出去的**"时才出现 —— 走委派的现有技能
    都没有 references，有 references 的真技能又被主 agent 自己干了，所以一直没暴露；
    `EXEC_PATH=delegate` 一开，第一次跑就撞上。

    ⚠️ **不用 `【…】`**（那是**段名的专用符号**，独占行首就等于声明"这是个段名"，
    而记录层靠这两条做确定性切分）—— 它是【技能指令】**里面**的一段，不是和它平级的段。

    ⚠️ **有才带这一段** —— 没有就返回空串（**没有就是没有**，不写"（无参考文件）"这种废话）。
    """
    files = skill_file_index_lines(skill_name)
    if not files:
        return ""
    return (
        f"\n\n**技能自带文件** —— 要用哪一份就用 `read_skill_file` 读全文："
        f"**`skill_name` 传「{skill_name}」**，`path` 照抄下面的清单"
        "（**不要凭文件名猜内容**）：\n" + "\n".join(files)
    )


def load_skill_file(skill_name: str, path: str) -> str | None:
    """读技能包里某个文件的全文；**不在清单里就返回 `None`**（见模块 docstring 的不变量）。

    `path` 是**相对技能包根**的路径（`references/xxx.md`），和清单给的一致。
    """
    if path not in {name for name, _ in list_skill_files(skill_name)}:
        return None
    return (PACK_DIRS[skill_name] / path).read_text(encoding="utf-8", errors="replace")


@tool
def read_skill(skill_name: str) -> str:
    """按需读一个技能的**完整指令**（渐进式披露第二层），附带它自带哪些文件。

    常驻在你眼前的只有技能名和一句话描述。写任务要求前若不确定这个技能
    **交付什么、按什么标准交付**，用它读原文核对 —— **不要凭描述猜**。
    """
    # 🔴 这里原来是 `from proto.agent import SKILLS`（领域层反向依赖编排层，
    #    就为了读到"评测替换过的那份表"）。注册表独立成模块后，那个环和这条
    #    lazy import 一起消失了 —— 见 `proto/registry.py` 的模块 docstring。
    # 🔴 **2026-11-05 从 `skills.py` 搬来这里**（F2/F3）：它同时要用"领域对象"
    #    （`registry`）和"磁盘 I/O"（本模块的文件清单）—— 住在 `skills.py` 就得
    #    **函数内 lazy import** 本模块（一提就成环，探针 `tmp/probe_cycle_skills_pack.py`
    #    实测：提到顶层当场 ImportError）。搬来和 `read_skill_file` 作伴：环没了，
    #    两个读工具也住在同一处。
    skill = registry.get(skill_name)
    if skill is None:
        return f"没有技能「{skill_name}」。可用：{'、'.join(registry.names())}"
    # ⚠️ **不用 `【…】`**（那是段名的专用符号）—— 它是**工具返回的头**，不是段名。
    out = f"**技能「{skill.name}」** —— {skill.description}\n\n{skill.instructions}"
    # 参考文件清单**现场扫盘**（不缓存在 Skill 上，见本模块 docstring）。
    # 提示块是**唯一一份**（`skill_file_prompt` —— 任务书那边用的是同一个函数）；
    # 没有自带文件时它返回空串（**一个字不加**）。
    out += skill_file_prompt(skill.name)
    return out


@tool
def read_skill_file(skill_name: str, path: str) -> str:
    """读技能包里某个**文件的全文**（渐进式披露第三层）。

    技能自带的规范 / 模板 / 标准（如《教案结构模板》《评分标准》《学科课标》）——
    路径就是 `SKILL.md` 里写的那个（形如 `references/xxx.md`）。
    清单在 `read_skill` 的返回里（派给执行器的任务书里也有一份）。

    **确定这一份确实用得上再读** —— 别把几份都拉进上下文。
    """
    have = skill_file_index_lines(skill_name)
    if not have:
        return f"技能「{skill_name}」没有自带文件。"
    got = load_skill_file(skill_name, path)
    if got is None:
        return f"「{skill_name}」里没有文件「{path}」。现有：\n" + "\n".join(have)
    # ⚠️ **不用 `【…】`**（那是段名的专用符号）—— 它是**工具返回的头**，不是段名。
    return f"**{skill_name} / {path}**\n\n{got}"


# ──────────────────────────── 加载 ────────────────────────────


def load_skill_pack(pack_dir: Path) -> tuple[Skill | None, list[str]]:
    """读一个技能包目录 →（`Skill`，说明/警告）。

    返回 `None` 表示"这个目录不是技能包"（没有 `SKILL.md`，或 frontmatter 没写 `name`）——
    **不是错误**，调用方跳过即可。参考文件**不在这里读**，只记下包目录。
    """
    entry = pack_dir / ENTRY_NAME
    if not entry.is_file():
        return None, []
    meta, body = split_frontmatter(entry.read_text(encoding="utf-8"))
    name = str(meta.get("name") or "").strip()
    if not name:
        return None, [f"{pack_dir.name}/{ENTRY_NAME} 的 frontmatter 没写 name，跳过"]
    PACK_DIRS[name] = pack_dir
    return (
        Skill(
            name=name,
            description=str(meta.get("description") or "").strip(),
            instructions=body,
        ),
        [],
    )


def load_skills_dir(path: str | Path) -> tuple[dict[str, Skill], list[str]]:
    """扫一个目录下的**所有技能包** →（`{name: Skill}`，说明/警告）。

    ⚠️ **只认子目录里的 `SKILL.md`**，目录下的散装 `*.md` 一律不看 ——
    一个技能是一个包，散文件没有"包"的边界。
    """
    root = Path(path)
    skills: dict[str, Skill] = {}
    warn: list[str] = []
    if not root.is_dir():
        return skills, [f"技能目录不存在：{root}"]
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        skill, w = load_skill_pack(d)
        warn += w
        if skill is not None:
            skills[skill.name] = skill
    return skills, warn
