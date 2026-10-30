"""**技能包** —— 文件夹形态的技能，以及它的参考文件（I/O + 格式）。

一个技能 = 一个文件夹：

    lesson-prep/
    ├── SKILL.md               ← 入口，**只有这一个名字**；frontmatter 给 name/description，正文即 instructions
    └── references/            ← **可选**，没有就没有（不是错误）
        ├── 教学目标编写指南.md
        └── 教案结构模板.md

## 🔴 参考文件**不进 `Skill` 对象**

它本来就在磁盘上读得到 —— 装进对象里是**多余的一份**。
`Skill` 因此仍然只有 `name` + `description` + `instructions`（用户 2026-10-30 定的，
也是 §0.6 C2「平台尽量少约束 skill 作者」和评审 D1「不引入任何新字段」的延续）。

**读不读、什么时候读、读哪一份，是 LLM 自己的决策** —— 这里只提供两件事：
`list_references()`（有哪些）和 `read_reference()`（读一份）。两者**都现场扫盘**，
内存里不留副本。

## 只认 `SKILL.md`

入口文件名**只有一种**。不做"跟目录同名"的兼容 ——
两种写法并存会让"这个目录到底是不是一个技能包"变得要靠猜。

## 参考文件的收与不收

**收**：扩展名在白名单里的文本文件（`TEXT_EXTS`），含子目录（名字带相对路径）。
**不收**：二进制、超大文件 —— **在"列"这一步就过滤掉**，所以 LLM 的清单里
根本不会出现读不了的东西。

> 🔑 **一条不变量**：`read_reference` **只认 `list_references` 给得出来的东西**。
> 路径穿越（`../`）、二进制、超大文件，全被这一条挡住 —— 不需要再写一套路径检查。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from langchain.tools import tool

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
#: 所以它不是 `Skill` 的字段（`Skill` 只管"这个能力是什么"，不管"它在哪个盘上"）。
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


# ──────────────────────────── 参考文件：列 + 读 ────────────────────────────


def list_references(skill_name: str) -> list[tuple[str, int]]:
    """这个技能包里有哪些参考文件 → `[(相对路径, 字数), ...]`。**现场扫盘，不缓存。**

    没有技能包、没有 `references/`、目录里全是二进制 —— 一律返回**空列表**：
    **没有就是没有，不是错误**。
    """
    pack = PACK_DIRS.get(skill_name)
    if pack is None:
        return []
    ref_dir = pack / REFERENCES_DIR
    if not ref_dir.is_dir():
        return []
    out: list[tuple[str, int]] = []
    for p in sorted(ref_dir.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in TEXT_EXTS:
            continue
        if p.stat().st_size > MAX_REF_BYTES:
            continue
        # 名字用**相对 `references/` 的路径**（子目录会带 `/`），读的时候原样传回来
        out.append((p.relative_to(ref_dir).as_posix(), len(p.read_text(encoding="utf-8", errors="replace"))))
    return out


def reference_index_lines(skill_name: str) -> list[str]:
    """参考文件清单（渐进式披露第二层半）：文件名 + 字数，**不含正文**。"""
    return [f"- {name}（{n} 字）" for name, n in list_references(skill_name)]


def read_reference_file(skill_name: str, ref_name: str) -> str | None:
    """读一份参考文件的全文；**不在清单里就返回 `None`**（见模块 docstring 的不变量）。"""
    if ref_name not in {name for name, _ in list_references(skill_name)}:
        return None
    pack = PACK_DIRS[skill_name]
    return (pack / REFERENCES_DIR / ref_name).read_text(encoding="utf-8", errors="replace")


@tool
def read_reference(skill_name: str, ref_name: str) -> str:
    """读技能包里某个**参考文件**的全文（渐进式披露第三层）。

    参考文件是技能自带的规范 / 模板 / 标准（如《教案结构模板》《评分标准》）。
    先看清单（`read_skill` 的返回里有，或 `list_references`），
    **确定这一份确实用得上再读** —— 别把几份都拉进上下文。
    """
    have = reference_index_lines(skill_name)
    if not have:
        return f"能力「{skill_name}」没有参考文件。"
    got = read_reference_file(skill_name, ref_name)
    if got is None:
        return f"「{skill_name}」里没有参考文件「{ref_name}」。现有：\n" + "\n".join(have)
    return f"【{skill_name} / {ref_name}】\n{got}"


@tool
def list_references_tool(skill_name: str) -> str:
    """列出某个技能包里有哪些**参考文件**（名字 + 字数）。**不含正文。**

    不确定这个能力带没带参考资料、或者想不起清单时用它。
    """
    have = reference_index_lines(skill_name)
    return f"能力「{skill_name}」的参考文件：\n" + "\n".join(have) if have else f"能力「{skill_name}」没有参考文件。"


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
