"""**推回去**（平台 → 执行器）：判定说"没完成"时，喂回去让它接着干。

> 用户要求：*"尽可能暴露一些有用信息到外部，不然你只是说没完成，
> 后续即使回去继续 react 也不知道该怎么做。"*

所以这段**不是**"你还没做完"，而是把判定吐出的 `progress` / `missing`
**原样**带上 —— 让它知道"已经做到哪、还差什么"，而不是从头再来。

⚠️ 数据来自 `judgement.py` 的 `_Judgement`（同一个包，无环）。
"""

from __future__ import annotations

from proto.handoff.judgement import _Judgement


def nudge_text(j: _Judgement) -> str:
    """**推它继续**时说什么 —— 把判定吐出的 `progress` / `missing` 都喂回去。"""
    parts: list[str] = ["任务还没完成。"]
    if j.progress:
        parts.append(f"**你已经完成了**（判定说）：{j.progress}")
    if j.missing:
        parts.append("**还缺**（判定说）：\n" + "\n".join(f"- {x}" for x in j.missing))
    parts.append("请**接着往下做**，不要从头再来。")
    parts.append(
        "如果确实缺少必要信息才能继续，就**明确说明你需要什么**"
        "（缺哪个参数、要什么确认）；否则不要用「已完成」之类的话收尾。"
    )
    return "\n\n".join(parts)
