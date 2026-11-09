"""软断言 —— **只有必须语义判断的才走这里**（其余都在 `checks.py` 用代码算）。

在**父进程**里跑（子进程已经退出），输入是子进程录下来的快照。
每条 rubric 也是 `(run, turn, **args) -> (ok, detail)`，跟 `checks.py` 同一个签名。

⚠️ **评分调用失败时判「不通过」，不判「通过」。**
判不了 ≠ 合格 —— 宁可标成未验证，也不能给假绿灯。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from proto import aio
from proto.llm import safe_structured

Result = tuple[bool, str]
Rubric = Callable[..., Result]

RUBRICS: dict[str, Rubric] = {}

#: 跑批结果根目录 —— `run.py` 开跑前设成 `runs/<tag>`。
#: 🔴 软判断的**输入输出必须留档**（用户要求："软硬判断尤为关键"）：
#: 判错了要能看出是"模型判错"还是"我们喂给它的材料不对"—— 后者更常见。
#: 留档**并进该 run 的 `_run.md`**（用户要求：日志不要搞几个文件，放一起）。
RUNS_DIR: Path | None = None


def _log(run: dict, name: str, prompt: str, result: object) -> None:
    """把一次评分的完整输入与输出，追加到**这个 run 自己的**运行全记录里。"""
    if RUNS_DIR is None:
        return
    d = RUNS_DIR / "artifacts" / f"{run.get('case')}__r{run.get('rep')}"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "_run.md").open("a", encoding="utf-8") as fh:
        # ⚠️ 用**四个**反引号围栏：prompt 里常含 ``` （产物正文、技能指令里都有）
        fh.write(
            f"\n\n## 软判断 · `{name}`\n\n"
            f"判错了先看这里：是**模型判错**，还是**喂给它的材料不对**。\n\n"
            f"````\n--- PROMPT（发给评分模型的全文）---\n{prompt}\n"
            f"\n--- RESPONSE ---\n{result}\n````\n"
        )


def _reg(fn: Rubric) -> Rubric:
    RUBRICS[fn.__name__.removeprefix("rubric_")] = fn
    return fn


def _history(run: dict, turn: dict) -> str:
    """到该轮为止的**用户原话** —— 判"用户是不是已经说过了"必须看这个。"""
    return "\n".join(f"用户第 {t['turn']} 轮：{t['user']}" for t in run["turns"][: turn["turn"]])


#: 评分材料里每条 `report` 的字符预算（保头保尾）。**这是"保尾"的**，见 `_clip_report`。
_RUBRIC_REPORT_BUDGET = 800


def _clip_report(text: str, budget: int = _RUBRIC_REPORT_BUDGET) -> str:
    """评分材料里的 `report` —— **保头保尾 ＋ 显式标注**。

    🔴 **原来是 `report[:300]`：只切头、无标记 —— 而执行器的问句通常写在末尾**。
    ⚠️ 这句话原来在 `evals/chat.py` 的注释里，2026-11-03 随旧播报层一起删了 ——
    **事实本身仍然成立**，所以它现在由这里承载（别再去 chat.py 找那句话）。
    于是"助手有没有向用户索要 X"这条 rubric 看不到问句 → 判"没在问" → **软断言假绿**。
    实测（2026-11-03，`handoff-verify`）：3 条 report 里 **2 条正好 300 字**，
    切在句中、**没有任何标记**。

    同文件的 `_clip_head` 是"只保头 + 标记"，**这里不能用它** —— 方向是反的。
    """
    text = str(text or "").strip()
    if len(text) <= budget:
        return text
    head = int(budget * 0.6)
    tail = budget - head
    return (
        f"{text[:head]}\n"
        f"……（平台截断：中间 {len(text) - budget} 字未展示给你；**问句常在末尾**，"
        f"看不到就别据此判「没在问」）……\n"
        f"{text[-tail:]}"
    )


def _reports(turn: dict) -> str:
    ts = turn.get("tasks") or []
    if not ts:
        return "（无）"
    return "\n".join(
        f"[{t['id']} {t['skill']} {t['stage']}] 执行器说：{_clip_report(t.get('report', ''))}"
        for t in ts
    )


# ──────────────────────────── rubrics ────────────────────────────


class _RepeatQuestion(BaseModel):
    """**哪些字段被重复索要了** —— 两个**平行**数组，一一对应。

    🔴 **必须是扁平的**（2026-11-09 实测）：原来写成 `items: list[_FieldAsked]`
    —— **嵌套**一个子模型 ⇒ Pydantic 生成 `$defs` ＋ `$ref` ⇒
    **DashScope 的 JSON-schema 转换器解析不了**：
    `400 … json_schema_converter.cc:4181: Cannot find field $defs in #/$defs/_FieldAsked`。
    后果正是上面警告过的那种：**调用失败 → 返回 False → 假红**（`judge` 里另外四个
    用扁平 schema 的 rubric 都正常 —— 只有这一个每次都挂）。
    ⇒ 拆成 `fields` ＋ `asked` 两个**平行数组**：语义一模一样（"没问"照样有正规写法
    `false`），但**没有嵌套**。〔沿革 2〕

    ⚠️ **别改回嵌套**（哪怕"更类型安全"）—— 服务端不认，改了就是全线假红。
    """

    fields: list[str] = Field(
        default_factory=list,
        description="给定列表里的**每一个**字段，**原样照抄**、顺序不变",
    )
    asked: list[bool] = Field(
        default_factory=list,
        description="与 `fields` **一一对应**：助手确实在向用户索要它 → true；没索要 → false",
    )


@_reg
def rubric_no_repeat_question(run: dict, turn: dict, fields: list[str]) -> Result:
    """**主 agent 或执行器有没有向用户索要这些字段**（用户其实已经说过了）。

    ⚠️ 为什么不能靠代码算：中文里「三年级」包含子串「年级」，
    子串匹配必然误伤。这是少数**必须**语义判断的地方。

    🔴 **2026-11-05 修 schema**（实测踩到假红）：原来 `asked: list[str]` ——
    模型想表达"没问"时**无处可写**，只好把否定塞进那个肯定列表里：
    实测拿到 `asked=['年级没有索要', '课时没有索要', '难度没有索要']` →
    断言读成"又向用户索要了" ✗ **假红**（判对了却标红，比漏判更坏：
    它会让人去查一个没坏的东西）。现在**每个字段一条 `{field, asked}`** ——
    "没问"有了正规写法，模型不必再发明否定式 ✓。
    """
    prompt = (
        "下面是一段用户与助手的对话，以及助手内部派出去的工作向它索要的信息。\n\n"
        f"【用户说过的话】\n{_history(run, turn)}\n\n"
        f"【助手最后给用户的回复】\n{turn.get('reply', '') or '（空）'}\n\n"
        f"【助手内部派出去的工作在索要什么】\n{_reports(turn)}\n\n"
        f"请判断：**助手有没有向用户索要下面这些信息**？\n"
        f"字段：{'、'.join(fields)}\n\n"
        "注意：用户已经说过的信息，再问一遍就算「索要」。\n"
        "**每个字段都要给一条**：确实在向用户索要的 → `asked=true`；没索要的 → `asked=false`。\n"
        "（助手内部向它自己索要、但助手没转达给用户的，不算。）"
    )
    try:
        r = aio.run(safe_structured(_RepeatQuestion, prompt, None, tag=f"评分 · 有没有重复问 {fields}"))
    except Exception as e:  # noqa: BLE001
        _log(run, "no_repeat_question", prompt, f"⚠️ 调用失败：{e}")
        return False, f"⚠️ 评分调用失败，无法判定：{type(e).__name__}: {e}"
    _log(run, "no_repeat_question", prompt, r)
    if r is None:
        return False, "⚠️ 评分调用失败（两次都挂），无法判定"
    # ⚠️ 两个**平行**数组（见 `_RepeatQuestion` 的 docstring）—— 按位置配对，
    #    不认识的字段名（模型自由发挥）也照收：报出来比吞掉强。
    asked = [f for f, a in zip(r.fields or [], r.asked or []) if a]
    if asked:
        return False, f"又向用户索要了：{asked}"
    return True, f"没有重复索要 {fields}"


def _clip_head(text: str, n: int) -> str:
    """取开头 n 字并显式标注截断 —— 判分看不到全文时要让它知道。"""
    text = text.strip()
    return text if len(text) <= n else text[:n] + f"\n……（后面还有 {len(text) - n} 字未展示）"


class _Derives(BaseModel):
    derived: list[str] = Field(default_factory=list, description="确实基于源产物的产物 ID")
    generic: list[str] = Field(
        default_factory=list, description="看不出与源产物关系（像通用件）的产物 ID"
    )
    evidence: str = Field(default="", description="判定依据，摘原话")


@_reg
def rubric_artifact_derives_from_source(run: dict, turn: dict, source: str = "教案") -> Result:
    """后序产物**内容上**是不是真的基于源产物 —— 依赖成立的语义证据。

    代码判不了：课题词相同不代表环节对得上，"像不像基于它写的"只能语义判。
    """
    arts = run.get("artifact_final") or {}
    src_ids = [k for k, v in arts.items() if source in str(v.get("title") or "")]
    if not src_ids:
        return False, f"找不到标题含「{source}」的产物：{[v.get('title') for v in arts.values()]}"
    src_id = src_ids[0]
    others = [k for k in arts if k != src_id]
    if not others:
        return False, f"除 {src_id} 外没有别的产物，无从判依赖"

    src_block = _clip_head(str(arts[src_id].get("content") or ""), 2500)
    other_block = "\n\n".join(
        f"【产物 {k}】{arts[k].get('title', '')}\n{_clip_head(str(arts[k].get('content') or ''), 1500)}"
        for k in others
    )
    prompt = (
        "下面是一次多技能协作交付的产物。先交付的是源产物，之后交付的几份**被要求参考它**。\n\n"
        f"【源产物 {src_id}】{arts[src_id].get('title', '')}\n---\n{src_block}\n---\n\n"
        f"{other_block}\n\n"
        "请逐份判断：**后面这些产物，内容上是不是真的基于源产物？**\n"
        "- 基于它 = 课题/知识点一致，且结构或环节能对上（如导学单的活动对应教案的教学过程）；\n"
        "- 像通用件 = 换个课题也能用、看不出与源产物的具体对应关系。\n"
        "把产物 ID 分别列进 `derived` 与 `generic`。"
    )
    try:
        r = aio.run(safe_structured(_Derives, prompt, None, tag="评分 · 后序产物是否基于源产物"))
    except Exception as e:  # noqa: BLE001
        _log(run, "artifact_derives_from_source", prompt, f"⚠️ 调用失败：{e}")
        return False, f"⚠️ 评分调用失败，无法判定：{type(e).__name__}: {e}"
    _log(run, "artifact_derives_from_source", prompt, r)
    if r is None:
        return False, "⚠️ 评分调用失败（两次都挂），无法判定"

    ok = len(r.derived) >= len(others)
    detail = f"源 {src_id}，后序 {len(others)} 份：基于 {r.derived or '（无）'}"
    if r.generic:
        detail += f"；像通用件 {r.generic}"
    if r.evidence:
        detail += f"；依据：{r.evidence[:150]}"
    return ok, detail


class _ReplyIsSummary(BaseModel):
    pasted_body: bool = Field(description="回复里是不是**大段搬运**了产物正文（不是摘要）")
    evidence: str = Field(default="", description="原话摘录")


@_reg
def rubric_reply_is_summary(run: dict, turn: dict) -> Result:
    """**产物正文有没有被搬进主 agent 的回复** —— 隔离的另一面。

    委派完成后只该说「交付了哪几样、产物 ID、一句话摘要」；
    把正文贴进回复等于隔离白做。
    """
    arts = turn.get("artifacts") or {}
    if not arts:
        return True, "没有产物，不适用"
    listing = "\n".join(f"- {k}：{v} 字" for k, v in arts.items())
    prompt = (
        f"助手委派出去的工作产出了这些文件：\n{listing}\n\n"
        f"【助手给用户的回复】\n---\n{turn.get('reply', '')}\n---\n\n"
        "请判断：这条回复是**简短摘要**（说了交付了哪几样、产物 ID、一两句概括），\n"
        "还是**把产物正文大段搬运了过来**？\n"
        "判定标准：回复长度与产物体量明显不成比例、且内容就是产物的正文 → pasted_body=true。\n"
        "只是转述一两句要点、或列出产物 ID → false。"
    )
    try:
        r = aio.run(safe_structured(_ReplyIsSummary, prompt, None, tag="评分 · 回复是不是摘要"))
    except Exception as e:  # noqa: BLE001
        _log(run, "reply_is_summary", prompt, f"⚠️ 调用失败：{e}")
        return False, f"⚠️ 评分调用失败，无法判定：{type(e).__name__}: {e}"
    _log(run, "reply_is_summary", prompt, r)
    if r is None:
        return False, "⚠️ 评分调用失败（两次都挂），无法判定"
    return (not r.pasted_body), ("回复搬运了产物正文：" + r.evidence[:150]) if r.pasted_body else "回复是摘要，没搬正文"


class _IsComplete(BaseModel):
    unfinished: list[str] = Field(
        default_factory=list, description="确实没写完的产物 ID（模板残留 / 留了以后再说 / 半截）"
    )
    evidence: str = Field(default="", description="判定依据，摘原话")


@_reg
def rubric_artifact_is_complete(run: dict, turn: dict, budget: int = 2500) -> Result:
    """产物**写完了没有** —— 模板残留 / 占位符 / 半成品。**只能语义判**。

    🔴 2026-11-05：这条原来是**代码侧找词**（`check_artifact_no_placeholder`：在产物正文里
    搜「待补充」「TODO」「此处省略」…）。两个毛病：
      ① 它断言的是**模型吐出来的东西** —— 模型换个写法（"稍后补""此处从略"）就漏 ✗；
         而正文里**引用**了这几个字的正常产物又会被误伤 ✗ —— **过了不代表对，挂了不代表错**；
      ② 词表只能越加越长（case 里还要按技能追加"模板残留"的词 ✗）——
         那是把问题修得更聪明，不是修掉。
    "写完了没有"本来就是语义判断（判定规则第 1 条一直这么要求，它原来是判定的活）——
    所以搬到这里：让评分模型读产物正文判，**判断过程留档**（`_log`，判错了能看出是模型判错
    还是喂的材料不对）。
    """
    arts = run.get("artifact_final") or {}
    if not arts:
        return False, "没有任何产物"
    blocks = "\n\n".join(
        f"【产物 {aid}】{a.get('title', '')}（{len(str(a.get('content') or ''))} 字）\n"
        f"---\n{_clip_head(str(a.get('content') or ''), budget)}\n---"
        for aid, a in arts.items()
    )
    prompt = (
        "下面是这次任务交付的产物正文（超长的保头并标注）。\n\n"
        f"{blocks}\n\n"
        "请判断：**这些产物是不是「写完了的成品」？**\n"
        "- 算**没写完**：模板残留没替换（如「（活动名称）」「XX」「【待定】」这种占位）、"
        "写着「待补充」「待填写」「此处省略」「TODO」这类留给以后的话、明显半截"
        "（列了小标题却没有内容、步骤只写到一半）。\n"
        "- 算**写完了**：正常的省略与概括、作者有意留的教学提示、正文里**引用**了这些词"
        "（比如在讲「怎么识别占位符」）。\n"
        "把**确实没写完**的产物 ID 列进 unfinished，摘原话；都写完了就留空。"
    )
    try:
        r = aio.run(safe_structured(_IsComplete, prompt, None, tag="评分 · 产物写完了没有"))
    except Exception as e:  # noqa: BLE001
        _log(run, "artifact_is_complete", prompt, f"⚠️ 调用失败：{e}")
        return False, f"⚠️ 评分调用失败，无法判定：{type(e).__name__}: {e}"
    _log(run, "artifact_is_complete", prompt, r)
    if r is None:
        return False, "⚠️ 评分调用失败（两次都挂），无法判定"
    if r.unfinished:
        return False, f"没写完：{'、'.join(r.unfinished)}（{r.evidence[:120]}）"
    return True, f"{len(arts)} 份产物都写完了"
