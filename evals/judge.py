"""软断言 —— **只有必须语义判断的才走这里**（其余都在 `checks.py` 用代码算）。

在**父进程**里跑（子进程已经退出），输入是子进程录下来的快照。
每条 rubric 也是 `(run, turn, **args) -> (ok, detail)`，跟 `checks.py` 同一个签名。

⚠️ **评分调用失败时判「不通过」，不判「通过」。**
判不了 ≠ 合格 —— 宁可标成未验证，也不能给假绿灯。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

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


def _reports(turn: dict) -> str:
    ts = turn.get("tasks") or []
    if not ts:
        return "（无）"
    return "\n".join(
        f"[{t['id']} {t['skill']} {t['stage']}] 执行器说：{t.get('report', '')[:300]}" for t in ts
    )


# ──────────────────────────── rubrics ────────────────────────────


class _RepeatQuestion(BaseModel):
    asked: list[str] = Field(
        default_factory=list,
        description="被向用户索要的字段，逐条列出（原话摘录）；一个都没有就留空",
    )


@_reg
def rubric_no_repeat_question(run: dict, turn: dict, fields: list[str]) -> Result:
    """**主 agent 或执行器有没有向用户索要这些字段**（用户其实已经说过了）。

    ⚠️ 为什么不能靠代码算：中文里「三年级」包含子串「年级」，
    子串匹配必然误伤。这是少数**必须**语义判断的地方。
    """
    prompt = (
        "下面是一段用户与助手的对话，以及助手内部派出去的工作向它索要的信息。\n\n"
        f"【用户说过的话】\n{_history(run, turn)}\n\n"
        f"【助手最后给用户的回复】\n{turn.get('reply', '') or '（空）'}\n\n"
        f"【助手内部派出去的工作在索要什么】\n{_reports(turn)}\n\n"
        f"请判断：**助手有没有向用户索要下面这些信息**？\n"
        f"字段：{'、'.join(fields)}\n\n"
        "注意：用户已经说过的信息，再问一遍就算「索要」。\n"
        "只把**确实在向用户索要**的字段列进 asked，摘原话；没有就留空。\n"
        "（助手内部向它自己索要、但助手没转达给用户的，不算。）"
    )
    try:
        r = safe_structured(_RepeatQuestion, prompt, None, tag=f"评分 · 有没有重复问 {fields}")
    except Exception as e:  # noqa: BLE001
        _log(run, "no_repeat_question", prompt, f"⚠️ 调用失败：{e}")
        return False, f"⚠️ 评分调用失败，无法判定：{type(e).__name__}: {e}"
    _log(run, "no_repeat_question", prompt, r)
    if r is None:
        return False, "⚠️ 评分调用失败（两次都挂），无法判定"
    if r.asked:
        return False, f"又向用户索要了：{r.asked}"
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
        r = safe_structured(_Derives, prompt, None, tag="评分 · 后序产物是否基于源产物")
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
        r = safe_structured(_ReplyIsSummary, prompt, None, tag="评分 · 回复是不是摘要")
    except Exception as e:  # noqa: BLE001
        _log(run, "reply_is_summary", prompt, f"⚠️ 调用失败：{e}")
        return False, f"⚠️ 评分调用失败，无法判定：{type(e).__name__}: {e}"
    _log(run, "reply_is_summary", prompt, r)
    if r is None:
        return False, "⚠️ 评分调用失败（两次都挂），无法判定"
    return (not r.pasted_body), ("回复搬运了产物正文：" + r.evidence[:150]) if r.pasted_body else "回复是摘要，没搬正文"
