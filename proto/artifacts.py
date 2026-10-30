"""**产物库** —— 运行期的交付物（"文件系统"的替身，**上下文隔离的落点**）。

产物正文写进来，只把 **ID** 交回主 agent；正文要读得显式 `read_artifact`。
执行器产出的东西因此**不会自动流进主 agent 的上下文** —— 这是整个架构的核心收益。

🔴 **和 `skills.py` 是两个领域**（2026-10-30 分开）：
  - **Skill** 是**静态能力包** —— 变化原因是"技能规范改了"
  - **产物** 是**运行期交付物** —— 变化原因是"交付机制改了"
两者原来挤在一个文件里（叫"Skill 定义 + 产物库"），边界不清。

**产物记录**：`title` / `summary` / `content` / `type`（**产出方声明的**文件类型）/
`ext` / `file`（+ 落盘后的 `path`）。类型走**硬性派发**（一张表），不嗅探内容。
"""

from __future__ import annotations

import itertools
import re
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
