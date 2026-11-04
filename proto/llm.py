"""LLM 调用的公共工具。

单独一个模块是为了避免 `agent` ↔ `executor` 循环依赖。
"""

from __future__ import annotations

import warnings
from typing import Any

from proto import trace
from proto.config import get_model

# 🔴 **压掉一条 pydantic 序列化警告**（2026-10-30 查清根因）。
#
# 现象：`UserWarning: Pydantic serializer warnings: PydanticSerializationUnexpectedValue(
# Expected `none` - serialized value may not be as expected [field_name='parsed',
# input_value=_Judgement(...)])`
#
# 根因（**实测确认，不是猜的**）：`trace.stream_agent` 用 `stream_mode="messages"`
# 之后，流式回调会传播到图内**所有** LLM 调用 —— 包括这里 `safe_structured` 的 `.invoke()`。
# 而 LangChain 在**流式**路径下会把 `with_structured_output` 的解析结果序列化，
# 它把 `additional_kwargs["parsed"]` 那个字段声明成 `None` 类型 —— 对不上，于是警告。
#
#   实测：同一个 runnable，`.invoke()` 不警告、`.stream()` 警告；**返回的结果是对的**。
#
# ⚠️ **只压这一条**（按消息正则匹配），**不要**用 `simplefilter("ignore")` 把别的警告一起埋了
# —— "看不到就观察不到"（用户原则）。真出别的问题时，这里的注释能指回去。
warnings.filterwarnings(
    "ignore", message=r".*Pydantic serializer warnings.*", category=UserWarning
)


def safe_structured(
    schema: type,
    prompt: str,
    default: Any,
    *,
    tag: str = "",
    kind: str = "",
    log_prompt: bool = True,
    log_response: bool = True,
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
        kind: 这次调用的**语义标签**（目前只有 `ask_main`）—— 原样发给 section 事件；
            它的 PROMPT / RESPONSE 块一律带 `kind="llm_io"`，展示层据此归并成同一块。
        log_prompt: 是否把提示词**逐字**写进日志。判定那类 prompt 里嵌着**产物正文**
            （提示词里嵌着产物正文那种），产物本身已另存 —— 用户要求
            "日志只告知生成了产物、给点产物信息，不必全文放在那里"，故此时传 False，
            由调用方先记一行"喂了什么"的结构化摘要。
        log_response: 是否把返回**原样**写进日志（`str(r)` ＝ pydantic repr）。
            ⚠️ 调用方**自己会渲染一条「决策记录」**时传 False —— 否则同一件事在日志里
            **两副面孔**：决策块里是排好版的「结果：can_answer = true / 补充：…」，
            下面又跟一条 `can_answer=True answer='课型：…\n学生基础：…'`（转义过的 repr）。
            用户 2026-11-03 定的规矩：**同一个东西不要搞两套** —— 显示给人看的
            和留在日志里的必须是同一份。
    """
    trace.section(tag or f"结构化输出 · {getattr(schema, '__name__', schema)}", kind=kind)
    if log_prompt:
        trace.block("PROMPT（发给 LLM 的全文）", prompt, kind="llm_io")
    else:
        trace.note(
            f"PROMPT 未逐字记录（共 {len(prompt)} 字）—— 内含**产物正文**，产物另存；"
            "喂了哪几份见上一行的「判定材料」",
            kind="llm_io" if kind else "",
        )
    import time

    t0 = time.time()
    for attempt in (1, 2):
        try:
            r = get_model().with_structured_output(schema).invoke(prompt)
            if log_response:
                trace.block(f"RESPONSE（{time.time() - t0:.1f}s）", str(r), kind="llm_io")
            return r
        except Exception as e:  # noqa: BLE001
            trace.note(f"⚠️ 结构化输出失败（第 {attempt}/2 次）：{type(e).__name__}: {e}",
                       kind="llm_io" if kind else "")
    trace.note(f"⚠️ 两次都失败 → 退化到保守默认值 {default}", kind="llm_io" if kind else "")
    return default
