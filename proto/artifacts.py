"""**产物库** —— 运行期的交付物（"文件系统"的替身，**上下文隔离的落点**）。

产物正文写进来，只把 **ID** 交回主 agent；正文要读得显式 `read_artifact`。
执行器产出的东西因此**不会自动流进主 agent 的上下文** —— 这是整个架构的核心收益。

**产物记录**：`title` / `summary` / `content` / `type`（**产出方声明的**文件类型）/
`ext` / `file`（+ 落盘后的 `path`）。类型走**硬性派发**（一张表），不嗅探内容。

## 这个文件是什么

产物这一层的**唯一持有者**：`_ARTIFACTS` 那张表只住在这里，对外**只有函数**
（`get` / `has` / `ids` / `items` / `count` / `chars` / `clear` / `store_artifact` /
`use_dir`），加上三个 LLM 工具（`write_artifact` / `read_artifact` / `list_artifacts`）。
**换实现（表 / 按会话分库 / 真存储）只动这个文件。**

## 设计原则（不变量）

1. **对外只有函数，不给 dict**（同 `proto/registry.py` 的铁律）—— 调用方只认这几个函数，
   表本身私有。〔沿革 2〕
   ⚠️ **导入本模块**（`from proto import artifacts`），别 `from proto.artifacts import get`
   —— 这些名字太通用，裸导入迟早撞名。
2. **类型走硬性派发，不嗅探内容**（`FILE_TYPES` 一张表）：说 md 就存 `.md`、说 html 就存
   `.html` —— 类型是**产出方声明的**（`write_artifact` 的 `file_type`）。〔沿革 1〕
   声明缺失 / 认不出来存 `DEFAULT_EXT`，**不报错**：产物已经产出来了，存不下比存错更糟。
3. **落盘 = 库里那份，一字不加** —— 正文就是正文，平台不必加料；"这是哪一份"由
   **文件名**（`art_001_<标题>.md`）承载。〔沿革 8〕
4. **ID 用发号器**（`itertools.count()`），不用 `len(表) + 1` —— 后者是"先查后写"，
   两步之间有窗口，产物会**静默丢失**。〔沿革 3〕
5. **落盘目录走 `use_dir()`，不直接赋值 `ARTIFACT_DIR`** —— 写错模块要**当场
   `AttributeError`**，不能挂在一个没人读的属性上。〔沿革 4〕
6. **`write_artifact` 按 run 记账**（`make_write_artifact` 的闭包 ＋ `created`）——
   **谁写的谁自己记**，不靠事后扫消息文本猜。〔沿革 7〕
7. **工具返回的头不用 `【…】`** —— 那是**段名的专用符号**（记录层靠"独占行首"做确定性
   切分）；它只是**工具返回的头**，标题用粗体。

## 关键取舍

**当场落盘**（`store_artifact` 顺手写文件）：产物是**交付物**，用户当场就要能打开 ——
`write_artifact` 那句"已写入产物 art_001"必须**指得到文件**。〔沿革 5〕

**清单带落盘路径，且放在"按需"那一层**（`artifact_index_lines`）—— 台账是**每轮注入**的
索引、必须短；这里是真调 `list_artifacts` 才付成本。〔沿革 6〕

## 沿革（改过什么、为什么改）

1. **产物领域从 `skills.py` 分出来**（2026-10-30）：**Skill** 是**静态技能包**（变化原因是
   "技能规范改了"）、**产物**是**运行期交付物**（变化原因是"交付机制改了"）—— 两者原来挤在
   一个文件里（叫"Skill 定义 + 产物库"），边界不清。
   同一天用户定了**硬性派发**：*"产物是什么 是生成产物的时候 ai 说的 跟什么梗概 title
   一起产出的 后续对产物的保存只是根据文件类型 硬性派发处理 你不要多余操作"* ——
   平台不嗅探内容，类型由产出方声明。
2. **对外收口成函数**（2026-11-03 审核）：这张表原来叫 `ARTIFACTS` 且**公开**，于是 9 个模块
   直接读写它（proto/ 6 个 ＋ evals/ 3 个）—— "换成真存储（表 / 按会话分库）"就成了 9 处
   散点改造，谁漏一处谁静默读旧库。守卫：`unit_artifact_race` ②。
3. **ID 换成发号器**（2026-10-30 实况）：给 `store_artifact` 加"当场落盘"时，**两步之间
   多插了活**（嗅探类型、拼文件名），窗口被撑开 —— `unit_artifact_race` 当场从 🟡"没撞上"
   翻成 🔴"撞了 18577 次"。`next()` 在 GIL 下是原子的，换掉就闭合了。
   ⚠️ 窗口在微秒级，端到端跑不到（3 个并行 delegate 的写入间隔 4–205 秒）——
   只能靠单元测试用 Barrier ＋ 拉满 GIL 切换频率放大出来。
4. **`use_dir()` 的由来**（2026-10-30 踩过，代价是"产物路径说了好几次都没加上"）：
   `ARTIFACT_DIR` 从 `skills.py` 搬到本模块之后，入口那句 `proto_skills.ARTIFACT_DIR = ...`
   **照样跑得通**（Python 允许给模块加新属性）—— 于是它挂在一个**没人读的**属性上，
   真正那份一直是 `None`：产物不落盘、`write_artifact` 的返回里没有路径，**而且一声不吭**。
5. **当场落盘**（2026-10-30 用户提的）：原来只在跑完时统一落盘，于是 `write_artifact` 那句
   "已写入产物 art_001"**指不到任何文件** —— 用户原话：*"保存地址要说一下 不然不清楚哪里找去"*。
6. **清单带落盘路径**（2026-11-04 日志定位）：主 agent 要答"文件在哪"，而它在**投递重跑**
   （`feed`）那条路上**没有回执**（回执只属于 `delegate` 工具调用，见 `briefing.Task.run_by`），
   台账的完成行又只有 ID —— 实测后果：主 agent 报给用户的是**它自己编的** `art_001.md`
   （真实文件叫 `art_001_荷花-教案.md`）。
7. **`make_write_artifact` 按 run 记账**（2026-11-03，第 5 步）：执行器要回答"**这次 run 写了
   哪些产物**"（`outcome.artifacts`）—— 原来靠**扫消息文本**猜（`_recover_artifacts`），
   再用 `preexisting` 差集补"读来的被算成写出来的"：既不是结构化、也不是硬机制，
   而且差集本身还留着并发窗口。
8. **落盘不再加"来源头"**（2026-11-06，外测 SX-13）：原来非 HTML 产物会加一个 `# {title}`
   ＋ id/字数 ＋ `---` 的头 —— 那个 H1 **冒充了文档标题**：技能模板的正文首行是
   `## 《课题》导学单`（H2），于是产物首部出现**两个同名标题**（实测：第 1 行 H1、
   第 7 行 H2，转 PDF / 导入教学系统时重复）。HTML 早就是"原样"的（加头会被当正文渲染）——
   **md 同理**，当时只对 HTML 想通了。
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path

from langchain.tools import tool


# ──────────────────────────── 产物类型（声明 + 硬性派发） ────────────────────────────

#: **声明的文件类型 → 扩展名** —— 平台据此**硬性派发**保存：说 md 就存 `.md`，
#: 说 html 就存 `.html`。**不嗅探内容** —— 类型是**产出方在交付时说的**。〔沿革 1〕
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
#
# 🔴 **对外只有函数，不给 dict**（模块头原则 1，同 `proto/registry.py` 的铁律）：
# **换实现只动这个文件**，调用方只认下面这几个函数。〔沿革 2〕
#
# ⚠️ **导入本模块**（`from proto import artifacts`），别 `from proto.artifacts import get`
#    —— 这些名字太通用，裸导入迟早撞名。

_ARTIFACTS: dict[str, dict[str, str]] = {}


def get(aid: str) -> dict[str, str] | None:
    """按 ID 取**产物记录**（含正文）；没有就 None。

    ⚠️ 返回的是**记录本身**（不复制）—— 读没问题；写请走 `store_artifact`。
    """
    return _ARTIFACTS.get(aid)


def has(aid: str) -> bool:
    """这条产物在不在库里（"在不在"与"内容是什么"是两个问题，分开问）。"""
    return aid in _ARTIFACTS


def ids() -> list[str]:
    """全部产物 ID（顺序 = 写入顺序）。"""
    return list(_ARTIFACTS)


def items() -> list[tuple[str, dict[str, str]]]:
    """（ID, 记录）的**快照** —— 遍历用，加进来的新产物不会中途冒出来。"""
    return list(_ARTIFACTS.items())


def count() -> int:
    return len(_ARTIFACTS)


def chars() -> int:
    """产物库正文合计字数 —— **隔离效果的读数之一**（`snapshot` / `__main__` 用它）。"""
    return sum(len(a["content"]) for a in _ARTIFACTS.values())


def clear() -> None:
    """清空 —— 跑批每个 case 开始前调它（`harness.install`）。"""
    _ARTIFACTS.clear()

#: 产物 ID 的**发号器** —— 用 `itertools.count()` 而不是 `len(表) + 1`。
#:
#: 🔴 **`len(表) + 1` 是"先查后写"，两步之间有窗口**：两个执行器同时算会拿到**同一个 ID**，
#: 后写覆盖先写 —— 产物**静默丢失**。`next()` 在 GIL 下是原子的，换掉就闭合了。
#: ⚠️ 窗口在微秒级、端到端跑不到 —— 只能靠 `unit_artifact_race` 用 Barrier ＋ 拉满 GIL
#: 切换频率放大出来。〔沿革 3〕
#: （ID 因此不再等于"库里第几个"—— 没有任何地方依赖这个。）
_ART_SEQ = itertools.count(1)

#: 产物**落盘到哪** —— 由入口设（`evals.chat` 设成本次 run 的产物目录）。
#: 不设 = 只进内存，最后由 `dump_artifacts` 统一落盘（跑批那条路径就是这样）。
#: ⚠️ **别直接赋值**，用 `use_dir()` —— 见它的 docstring。
ARTIFACT_DIR: Path | None = None


def use_dir(path: Path) -> None:
    """告诉产物库"这次 run 的产物放哪"。**入口调这个，不要直接赋值 `ARTIFACT_DIR`。**

    🔴 **为什么要有这个函数**（模块头原则 5）：走函数，**写错模块会当场 `AttributeError`**
    （启动就炸，不用等人发现）。〔沿革 4〕
    """
    global ARTIFACT_DIR
    ARTIFACT_DIR = path


def store_artifact(title: str, summary: str, file_type: str, content: str) -> str:
    """把产物落进产物库，返回产物 ID。**顺手落盘**（设了 `ARTIFACT_DIR` 的话）。

    🔴 **为什么当场落盘**（模块头「关键取舍」）：产物是**交付物**，用户当场就要能打开 ——
    `write_artifact` 那句"已写入产物 art_001"必须**指得到文件**。〔沿革 5〕

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
        # 🔴 **落盘 = 库里那份，一字不加**（模块头原则 3）：正文就是正文，平台不必加料；
        #    "这是哪一份"由**文件名**（`art_001_<标题>.md`）承载，正文里不需要再来一遍。〔沿革 8〕
        p.write_text(rec["content"], encoding="utf-8")
        rec["path"] = str(p)
    _ARTIFACTS[aid] = rec
    return aid


def make_write_artifact(created: list[str] | None = None):
    """造一个 `write_artifact` 工具。`created` 非空时，**顺手把产物 ID 记进去**。

    🔴 **为什么要"造"而不是共用一个**（模块头原则 6）：执行器要回答"**这次 run 写了哪些
    产物**"（`outcome.artifacts`）—— 改成**记账**：谁写的，谁自己记。〔沿革 7〕

    ⚠️ **用闭包，不用模块级全局**：并行 delegate 时两个执行器会互相覆盖 ——
    同 `executor._user_turn_reader` 的教训（`ARTIFACT_DIR` 那种全局状态在这儿就是坑）。
    闭包 ＝ **按 run 归属**，并发窗口**结构上不存在**。
    """

    @tool
    def write_artifact(title: str, summary: str, file_type: str, content: str) -> str:
        """把一份**产物**写进产物库。正文不会再回到主 agent 的上下文，只返回产物 ID。

        Args:
            title: 产物名。
            summary: **一句话梗概** —— 它是产物对外露出的那一层：主 agent 靠它决定
                要不要读全文、要不要把它传给下一个技能。不写梗概，别人只能读全文。
            file_type: **文件类型**（`md` / `html`）—— 由**产出方**在交付时一起说，
                平台据此**硬性按类型保存**：说 md 就存 `.md`，说 html 就存 `.html`。
            content: 产物正文全文。
        """
        aid = store_artifact(title, summary, file_type, content)
        if created is not None:
            created.append(aid)
        a = _ARTIFACTS[aid]
        # 返回里**带上文件类型和保存地址** —— 用户和后面的代码都要靠它找到这份产物
        where = f"\n文件：{a['path']}" if a.get("path") else ""
        return f"已写入产物 {aid}（{len(content)} 字，{a['ext'].lstrip('.')}）{where}"

    return write_artifact


#: 主 agent 用的那个（它不需要按 run 记账 —— 它写的东西直接进自己的对话）
write_artifact = make_write_artifact()


@tool
def read_artifact(artifact_id: str) -> str:
    """按需读回产物全文（渐进式披露第二层）。

    ⚠️ **读了就进你的上下文**，长产物会一直占着。只有确实要看内容时才读；
    只是把它交给下一个任务，用 `delegate(..., artifacts=[...])` 传 ID 就够了。
    """
    a = get(artifact_id)
    # ⚠️ **不用 `【…】`**（段名专用符号，见模块头原则 7）—— 它是**工具返回的头**，不是段名。
    return f"**{a['title']}**\n\n{a['content']}" if a else f"产物 {artifact_id} 不存在"


def artifact_index_lines() -> list[str]:
    """产物清单（渐进式披露第一层）：ID / 名称 / 字数 / **类型** / 梗概 / **落盘路径**，
    **不含正文**。

    🔴 **路径必须在这一层**（模块头「关键取舍」）：主 agent 要答"文件在哪"，而它在
    **投递重跑**（`feed`）那条路上**没有回执**（回执只属于 `delegate` 工具调用，
    见 `briefing.Task.run_by`），台账的完成行又只有 ID。〔沿革 6〕

    ⚠️ 放在**这一层**而不是台账：台账是每轮注入的索引，必须短；这里是**按需**的
    （只有真调 `list_artifacts` 才付成本），和回执【产出的产物】是**同一个形状**
    （ID ＋ 梗概一行，路径另起一行缩进 2 格）。
    """
    return [
        f"- {aid}《{a['title']}》（{len(a['content'])} 字，{a.get('ext', '.md').lstrip('.')}）"
        f"—— {a.get('summary') or '（无梗概）'}"
        + (f"\n  文件：{a['path']}" if a.get("path") else "")
        for aid, a in _ARTIFACTS.items()
    ]


@tool
def list_artifacts() -> str:
    """列出本会话已交付的产物：ID、名称、字数、梗概、**落盘路径**。**不含正文。**

    要判断"该把哪份产物交给下一个任务"、或**要答"文件在哪"**时用它；
    正文按需用 `read_artifact` 读。
    """
    if not _ARTIFACTS:
        return "本会话还没有产物。"
    return "本会话已交付的产物：\n" + "\n".join(artifact_index_lines())
