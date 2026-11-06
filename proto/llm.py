"""LLM 调用的公共工具。

## 这个文件是什么

一个函数：`safe_structured()` —— 结构化输出调用 ＋ **兜底**（重试一次 → 仍失败退化到
调用方给的**保守默认值**）。另外在 import 时压掉一条已知的 pydantic 序列化警告。

单独一个模块是为了避免 `agent` ↔ `executor` 循环依赖。

## 设计原则（不变量）

1. **一次抖动不许炸掉整轮** —— 所以 `safe_structured` 有兜底；由来见那个函数的 docstring。
2. **退化值必须保守**（`default` 参数）—— 宁可"答不上 / 没做完"，也不编一个结论。
3. **只压这一条警告**（按消息正则匹配），**不要** `simplefilter("ignore")` 把别的警告
   一起埋了 —— "看不到就观察不到"（用户原则）；真出别的问题时，这里的注释能指回去。
4. **一套，别分叉**（用户定的规矩〔沿革 2〕）：显示给人看的和留在日志里的必须是同一份
   —— 所以调用方**自己会渲染一条「决策记录」**时，`log_response` 传 False。

## 沿革（改过什么、为什么改）

> 编号是**引用锚**（正文里写 `〔沿革 N〕`）—— **只追加、不改号**，新条目加在**末尾**。

1. **压掉一条 pydantic 序列化警告**（2026-10-30 查清根因）。
   现象：`UserWarning: Pydantic serializer warnings: PydanticSerializationUnexpectedValue(
   Expected `none` - serialized value may not be as expected [field_name='parsed',
   input_value=_Judgement(...)])`。
   根因（**实测确认，不是猜的**）：`trace.stream_agent` 用 `stream_mode="messages"`
   之后，流式回调会传播到图内**所有** LLM 调用 —— 包括这里 `safe_structured` 的
   `.invoke()`；而 LangChain 在**流式**路径下会把 `with_structured_output` 的解析结果
   序列化，它把 `additional_kwargs["parsed"]` 那个字段声明成 `None` 类型 —— 对不上，
   于是警告。实测：同一个 runnable，`.invoke()` 不警告、`.stream()` 警告；
   **返回的结果是对的**。
2. **`log_prompt` / `log_response` 的由来**（2026-11-03，用户定）。规矩是
   **同一个东西不要搞两套**：调用方自己会渲染「决策记录」时若不关掉，同一件事在日志里
   就有**两副面孔** —— 决策块里是排好版的「结果：can_answer = true / 补充：…」，
   下面又跟一条转义过的 repr（`can_answer=True answer='课型：…'`）。`log_prompt` 同理：
   判定那类 prompt 里嵌着**产物正文**，产物已另存，用户要求"日志只告知生成了产物、
   给点产物信息，不必全文放在那里"。
3. **计时起点挪进循环**（2026-11-05 修）。原来在**外面** —— 第二次尝试的
   `RESPONSE（N.Ns）` 显示的是**两次累计**的秒数，日志里那个数字会骗人
   （看起来"这次调用怎么这么慢"，其实第一次失败那段时间也被算进去了）。
4. **`None` 也算失败**（2026-11-05 加）。`with_structured_output` 在模型吐了空的时候
   可能**返回 `None` 而不抛** —— 那样调用方拿到 `None`，下一行（`j.status` /
   `ans.can_answer`）直接 AttributeError，**整轮炸**；而这条路上 `safe_structured`
   的全部意义就是"别让一次抖动炸掉整轮"。⚠️ **没实测到过**（拿不到稳定复现）——
   代价 2 行，收益是堵死这条 AttributeError。
5. **PROMPT 全文默认不逐字记**（2026-11-06，用户定）。`log_prompt` 原来只管"调用方
   允不允许"，而**默认是记** —— 于是试答那一步**每轮把主 agent 的整个上下文抄一遍**进
   日志。实测 4 次试答 **621 → 1225 → 1846 → 2411 字**，
   **无上界**（它 ∝ 会话长度）；同期交接书 289 → 824 字，**有界**（∝ 一个任务）。
   用户原话：*"日志能少打印一些吗 因为会越来越长 而且上下文本来就存在 如果实在要看就
   全量打出来 默认就不打印了 保持 decide 清晰 而且大小合适"*。
   ⇒ 拆成两个问题：**谁决定**（`log_prompt`，调用方声明"允许"）vs **什么时候看**
   （`config.log_prompt_verbatim`，默认不记）。
   ⚠️ 顺带修掉一句**已经变成假话**的注释：原来这里写"生产代码里暂时没有这样的调用方"
   —— 试答**就是**（它的 prompt 里嵌着主 agent 的全部上下文）。
   ⚠️ **不记也要说**（"切了要标"）：关掉时留一条 note 报字数 ＋ 开关。
"""

from __future__ import annotations

import time
import warnings
from typing import Any

from proto import trace
from proto.config import get_model, log_prompt_verbatim

# 🔴 **压掉一条已知的 pydantic 序列化警告** —— 它是 `stream_mode="messages"` 的副作用，
#    **返回的结果是对的**（现象 / 根因 / 实测见〔沿革 1〕）。
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
    """结构化输出调用 + **兜底**（重试一次 → 仍失败退化到 `default`）。

    ⚠️ 实测 DashScope 会偶发 `grammar validation or compilation failed:
    sandbox grammar compile timed out` —— 一次抖动就**炸掉整个 turn**（实测踩过两次）；
    这类基础设施抖动不该让整轮失败。

    Args:
        schema: Pydantic 模型类。
        prompt: 提示词全文。
        default: 重试仍失败时的返回值 —— **必须是保守的那个**。
        tag: 日志里显示的标题。
        kind: 这次调用的**语义标签**（目前只有 `ask_main`）—— 原样发给 section 事件；
            它的 PROMPT / RESPONSE 块一律带 `kind="llm_io"`，展示层据此归并成同一块。
        log_prompt: 这一步的 prompt **允不允许**逐字写进日志（调用方声明，默认允许）。
            **prompt 里嵌了产物正文时**传 False —— 产物本身已另存，由调用方先记一行
            "喂了什么"的结构化摘要即可；用户要求"日志只告知生成了产物、给点产物信息，
            不必全文放在那里"〔沿革 2〕。
            ⚠️ **它只管"允不允许"**；"这次运行要不要真记"是 `PROTO_LOG_PROMPT`（默认
            **不记**）—— 两个问题分开：**谁决定**（调用方）vs **什么时候看**（开关）〔沿革 5〕。
            ⚠️ 试答（`agent._ask_main`）就是"嵌了正文"的那个调用方：它的 prompt 里是
            **主 agent 的全部上下文**，而那是**逐条已记过**的 —— 它走默认（不逐字记）。
        log_response: 是否把返回**原样**写进日志（`str(r)` ＝ pydantic repr）。
            ⚠️ 调用方**自己会渲染一条「决策记录」**时传 False —— 否则同一件事在日志里
            **两副面孔**（〔沿革 2〕）。用户定的规矩：**同一个东西不要搞两套** ——
            显示给人看的和留在日志里的必须是同一份。
    """
    trace.section(tag or f"结构化输出 · {getattr(schema, '__name__', schema)}", kind=kind)
    # 🔴 **默认不逐字记**（渐进式披露：清单常驻、正文按需）—— 清单是调用方上面那条
    #    `trace.handoff` 摘要（**多少字 ＋ 什么构成**），正文要看时置 `PROTO_LOG_PROMPT=1`。
    #    为什么默认关、数据是多少，见 `config.log_prompt_verbatim` 的 docstring〔沿革 5〕。
    # ⚠️ **不记也必须说**（"切了要标"）—— 下面两条 note 就是那个声明，缺了才是违反。
    if log_prompt and log_prompt_verbatim():
        trace.block("PROMPT（发给 LLM 的全文）", prompt, kind="llm_io")
    elif log_prompt:
        # ⚠️ **别写"构成见上面那条交接摘要"**：`trace.handoff` 的行**只进日志、不上屏**
        #    （`chat.py` 那条总注释）—— 那样终端上就指了一个**不存在的东西**。
        #    "喂了什么"由调用方写在**两边都上屏**的地方（试答那步是【前提】那一格）。
        trace.note(
            f"PROMPT 未逐字记录（共 {len(prompt)} 字）—— 要看全量置 `PROTO_LOG_PROMPT=1`",
            kind="llm_io" if kind else "",
        )
    else:
        trace.note(
            f"PROMPT 未逐字记录（共 {len(prompt)} 字）—— 内含**产物正文**，产物另存；"
            "喂了哪几份见调用方上面记的那条摘要",
            kind="llm_io" if kind else "",
        )

    for attempt in (1, 2):
        # ⚠️ **计时起点在循环里**：放外面的话，第二次尝试的 `RESPONSE（N.Ns）`
        #    显示的是**两次累计**的秒数 —— 日志里那个数字会骗人〔沿革 3〕。
        t0 = time.time()
        try:
            r = get_model().with_structured_output(schema).invoke(prompt)
            # 🔴 **`None` 也算失败**：`with_structured_output` 在模型吐了空的时候可能
            #    **返回 `None` 而不抛** —— 那样调用方下一行（`j.status` /
            #    `ans.can_answer`）直接 AttributeError，**整轮炸**；而这条路上
            #    `safe_structured` 的全部意义就是"别让一次抖动炸掉整轮"。
            #    ⚠️ **没实测到过**（拿不到稳定复现）—— 代价 2 行，收益是堵死它〔沿革 4〕。
            if r is None:
                raise ValueError("结构化输出返回了 None（模型吐了空）")
            if log_response:
                trace.block(f"RESPONSE（{time.time() - t0:.1f}s）", str(r), kind="llm_io")
            return r
        except Exception as e:  # noqa: BLE001
            trace.note(f"⚠️ 结构化输出失败（第 {attempt}/2 次）：{type(e).__name__}: {e}",
                       kind="llm_io" if kind else "")
    trace.note(f"⚠️ 两次都失败 → 退化到保守默认值 {default}", kind="llm_io" if kind else "")
    return default
