"""LLM 调用的公共工具。

单独一个模块是为了避免 `agent` ↔ `executor` 循环依赖。
"""

from __future__ import annotations

from typing import Any

from proto import trace
from proto.config import get_model


def safe_structured(
    schema: type, prompt: str, default: Any, *, tag: str = "", log_prompt: bool = True
) -> Any:
    """结构化输出调用 + **兜底**。

    ⚠️ 实测 DashScope 会偶发 `grammar validation or compilation failed:
    sandbox grammar compile timed out` —— 一次抖动就**炸掉整个 turn**（实测踩过两次）。
    这类基础设施抖动不该让整轮失败，所以重试一次后退化到**保守默认值**。

    Args:
        schema: Pydantic 模型类。
        prompt: 提示词全文。
        default: 重试仍失败时的返回值 —— **必须是保守的那个**。
        tag: 日志里显示的标题。
        log_prompt: 是否把提示词**逐字**写进日志。判定那类 prompt 里嵌着**产物正文**
            （`_judge_material` 会把产物内容喂进去），产物本身已另存 —— 用户要求
            "日志只告知生成了产物、给点产物信息，不必全文放在那里"，故此时传 False，
            由调用方先记一行"喂了什么"的结构化摘要。
    """
    trace.section(tag or f"结构化输出 · {getattr(schema, '__name__', schema)}")
    if log_prompt:
        trace.block("PROMPT（发给 LLM 的全文）", prompt)
    else:
        trace.note(
            f"PROMPT 未逐字记录（共 {len(prompt)} 字）—— 内含**产物正文**，产物另存；"
            "喂了哪几份见上一行的「判定材料」"
        )
    import time

    t0 = time.time()
    for attempt in (1, 2):
        try:
            r = get_model().with_structured_output(schema).invoke(prompt)
            trace.block(f"RESPONSE（{time.time() - t0:.1f}s）", str(r))
            return r
        except Exception as e:  # noqa: BLE001
            trace.note(f"⚠️ 结构化输出失败（第 {attempt}/2 次）：{type(e).__name__}: {str(e)[:120]}")
    trace.note(f"⚠️ 两次都失败 → 退化到保守默认值 {default}")
    return default
