"""**跑 agent 图** —— 流式驱动 ＋ 抖动重试 ＋ 上游拒绝分类。

## 这个文件是什么

`stream_agent`：跑一张 agent 图，**每一步、每个字**都发成事件（记录器落盘、展示层上屏）。
顺带管两件传输层的事：**基础设施抖动重试一次**、**区分"确定性拒绝"**（4xx 除 408/429）。

    agent 图 ──stream_agent──▶ 事件总线（proto/trace.py）──▶ 记录器 / 展示层

## 设计原则（不变量）

1. **它只管"跑"，不管"记"** —— 发事件走总线（`trace.emit` / `trace.note`），
   落盘是 `proto/recorder.py` 的事。本模块**不碰文件**。
2. **重试只在"还没走完第一步"时**（`_stream_with_retry`）—— 从头重跑 = `state` 干净、
   **副作用不干净**（重跑会把已经做过的工具调用再做一遍）。〔`trace.py` 沿革 21〕
3. **确定性拒绝不重试**（`upstream_rejected`）—— 4xx（除 408/429）是上游对**这份内容**的
   拒绝，原样重发会再被拒。〔`trace.py` 沿革 28〕
   ⚠️ **别把它搬去 `llm.safe_structured`**：那条路上的 400 **就是它自己的抖动**
   （DashScope 的 grammar 编译超时，实测见 `logs/`），分类了就把唯一有效的重试掐掉。

## 关键取舍

**为什么从 `trace.py` 搬出来**（2026-11-09，第四轮外审 P0-2）。原来它和事件总线住一个文件
—— 用户：*"一个文件为啥要装两个完全不同的事啊"*。
⚠️ **审计给的三条理由都不成立**（我核过）：① 它说"记录层知道了模型超时"——`trace` 从
`config` 只拿 `app_root`；② 它说这里的重试和 `llm.safe_structured` 是"同一类策略必然分叉"
—— **方向反了**，两处本来就该不一样（见上面原则 3 的 ⚠️）；③ 它说"`stream_agent` 成了
事实上的 LLM 入口"——那是描述，不是缺陷。
⇒ 搬它的理由**只有一个**：**一个模块一个职责**。

## 沿革（改过什么、为什么改）

> 编号是**引用锚**（正文里写 `〔沿革 N〕`）—— **只追加、不改号**，新条目加在**末尾**。

1. **建它**（2026-11-09）：从 `proto/trace.py` 拆出来（第四轮外审 P0-2）。
   前史见 `trace.py` 沿革 4 / 5 / 6 / 18 / 20 / 21 / 22 / 23 / 28（编号不动 —— 那是引用锚）。
"""

from __future__ import annotations

from typing import Any

from proto import trace


def stream_agent(graph, state, config, who: str, *, role: str):
    """跑一个 agent 图，**每一步 + 每个字**都录下来 —— 边跑边发 `stream` 事件。

    Yields：每个 super-step 的完整 state（`stream_mode="values"` 那一支）。
    ⚠️ **第一个 chunk 是输入 state 本身**（先于任何 super-step；之后只有改了 state 的
    步才吐）—— 消费方别把它当成"走了一步"（`_stream_with_retry` 的重试边界靠这条，见那里）。

    🔴 **为什么要有它**：`invoke` 会把整个 agent 循环跑完才返回，于是"模型→工具→模型→
    工具…"这一串**全程静默**，用户侧就是**黑屏**，分不清"在干活"还是"卡住了"。〔沿革 4〕

    两层一起给：
      - **每个 super-step** → `values` 那一支，调用方拿去做逐步骤 dump
      - **每个字** → `stream` 事件（攒到换行发一行），记录器落盘、展示层实时上屏

    ⚠️ **前提：所有 `AIMessage` 都从这里流过** —— 所以记录器对 AI 的话
    **只写头部、不再重复正文**（正文已经在上面流出来了）。改调用点时记得这条。

    ⚠️ 多个执行器**并行**时，它们的事件会交错 —— 所以 `who` 要能分辨是谁
    （主 agent / 执行器·`<技能名>`）。

    ⚠️ **只发 `node='model'` 的 chunk** —— 工具内部的结构化调用（判定 / 试答）
    流出来是裸 JSON，既不可读、又和已解开的展示重复。〔沿革 5〕

    `role`：`trace.AGENT` / `trace.EXECUTOR`，**必填** —— 展示层按它归版式。
    `who` 只是显示名（`执行器·lesson-prep`），**随便改，不影响任何逻辑**。
    """
    # 🔴 校验在**生成器外面**：漏传 / 传错立刻炸，而不是等第一次 next()。
    return _stream_with_retry(graph, state, config, who, trace.check_role(role, "stream_agent"))


def upstream_rejected(e: BaseException) -> bool:
    """这个异常是不是「上游**确定性拒绝**」—— 4xx（除 408/429）？

    🔴 **为什么要有它**：4xx（除 408/429）是上游对**这份内容**的拒绝（如内容策略）——
    原样重发会**再被拒**：重试是白烧一次（大生成很贵），"原样再派"是**错的话**
    （该说"换措辞/改内容"，见 `handoff.FaultKind`）。〔沿革 28〕

    ⚠️ **按 `status_code` 判、不用 provider 的类型**：trace 层不认识 openai（换一家 SDK
    就失效）；`status_code` 是 HTTP 语义，鸭子类型就够。
    ⚠️ **说不准就当抖动**（保守，同现状）：没有 `status_code` 的异常（网络/超时/网关）
    照旧重试一次；**408（请求超时）/ 429（限流）是瞬时的**，也照旧重试
    （而且 SDK 自己已内重试过一轮，我们这一轮是第二道）。
    """
    code = getattr(e, "status_code", None)
    return isinstance(code, int) and 400 <= code < 500 and code not in (408, 429)


async def _stream_with_retry(graph, state, config, who: str, role: str):
    """跑 `_stream_agent`；基础设施抖动就重试一次（**边界见下**），仍失败才抛。

    🔴 **为什么兜在这一层**：这是**两条链唯一共用的缝** —— 主 agent 的循环、执行器的
    循环都从这过，放这儿两边一起有兜底，不会"改了一条忘了另一条"（`executor.py` /
    `agent.py` 全文一个 try/except 都没有）。〔沿革 20〕
      - **降级不在这里**：两条链该怎么退化不一样（执行器 → `run_task` 落"这轮没跑成"
        交主 agent 决定；主 agent → 由**入口**兜）。这里只做最保守的一步：
        重试一次，然后把异常**原样抛给调用方**。
      - 形状同 `llm.safe_structured`（重试一次 → 仍失败才退化）—— **先例在，别另立一套**。

    ⚠️ **"会话不死"只在交互式 CLI 成立**（`proto/__main__.py` 的 `turn()` 按轮捕获）；
    跑批入口（`evals/_child.py`）**不兜**，整轮记失败 —— 🔴 **这是定的，不是缺口**：
    跑批是**测试用途**，改成平台接住它**照样判红**（活没干完，断言过不了）。
    别再往 `node_agent` 里加兜底了 —— 见 `docs/solution.md` §五 **X7**。〔沿革 24〕

    **重试的边界：只在"还没走完第一步"时重试。**

    🔴 **从头重跑 = `state` 干净，副作用不干净**（实测见〔沿革 21〕）—— 所以第一个
    super-step 走完就不再重试：抖在那之前 = 什么都还没干，重试是**白赚**；抖在那之后 =
    已经动过手，**如实抛出去**比静默重做一遍强。
    ⇒ 边界是**数到第二个 values chunk**（`_stream_agent` **只在每步结束时** `yield`，
    流式的字是发 `stream` 事件、不走 yield；第一个 chunk 是输入 state，之后只有
    **改了 state 的步**才吐，所以"数到 2"＝"至少一步动过 state" —— 两条前提由
    `unit_stream_join` ⑦ 拿真图钉住）。

    ⚠️ **代价如实说**：落在"第二步之后"的抖动，这一轮就**真的失败了**（〔沿革 22〕）
    —— 执行器那条有 `run_task` 的 fault 出口接着；主 agent 那条由入口兜。

    📌 **更彻底的做法（从最后一个走完的 super-step 接着跑）没做** —— 见〔沿革 23〕。

    ⚠️ 已经流出去的字**不收回**（记录层是原始记录）—— 所以重试前先发一条 `retry` 事件，
      把"上面那半段作废、现在重来"说破；不然读日志的人会以为模型把话说重了。
    """
    #: 已经吐出去的 values chunk 数 —— ⚠️ **第一个是"输入 state 本身"**（真实图在任何
    #: super-step 执行之前先吐它；之后**只有改了 state 的步才吐** —— 两条都由
    #: `unit_stream_join` ⑦ 拿真图钉住）。所以"至少一步动过 state"＝数到 **2**。
    #: 🔴 原来这里是布尔 `stepped`（"吐过东西"）—— 被那个输入 chunk 提前置真，
    #: 重试对"第一步抖动"**从未生效**（守卫 `unit_stream_join` ④）。〔沿革 21〕
    chunks_seen = 0
    for attempt in (1, 2):
        try:
            async for chunk in _stream_agent(graph, state, config, who, role):
                chunks_seen += 1
                yield chunk
            return
        except Exception as e:  # noqa: BLE001 —— 抖动的种类不可穷举（网络/超时/限流/网关）
            # 🔴 **确定性拒绝不重试**：4xx（除 408/429）是上游对**这份内容**的拒绝 ——
            #    原样重发会再被拒，重试只是白烧一次（大生成很贵）。照实抛给调用方，
            #    由它按 `fault_kind="rejected"` 说人话（见 `upstream_rejected`）。〔沿革 28〕
            if attempt == 2 or chunks_seen >= 2 or upstream_rejected(e):
                raise
            # ⚠️ **走 `trace.note()` 不走 `trace.emit()`**（第三轮外审 P3-4）：这一处原来直调
            #    `_publish` 并**写死** `kind="retry"` —— 一个**真 kind 值**却绕过了
            #    `_check_kind`。改名 / 打错（`retrry`）时校验抓不到，只有展示层那道
            #    运行时自检会警告。其余直调处写的都是 `kind=""`（纯记录，安全），
            #    只有这一个是"真值走小路"。
            trace.note(f"⚠️ {who} 这一轮**基础设施抖动**（{type(e).__name__}: {e}）"
                 "—— 已重试一次（上面流出去的那半段作废）", kind="retry")


async def _stream_agent(graph, state, config, who: str, role: str):
    buf = ""
    cur_id: str | None = None      # 当前这条 AI 消息的 id —— 换了消息就把 buf 落盘

    def flush_line(line: str) -> None:
        if line.strip():
            trace.emit({"type": "stream", "kind": "", "role": role, "who": who,
                      "text": line, "ts": trace.stamp()})

    # 🔴 `try/finally` 是**必须的**：`buf` 是**生成器局部变量**，异常时不 flush 的话
    #    "这半行说了什么"在日志里**一个字都没有** —— 而 `retry` note 只说"上面那半段作废"，
    #    读日志的人看不到被作废的是什么，**等于记录层替模型说了假话**（判据②：切了必须标）。〔沿革 18〕
    #    ⚠️ 重试**不收回**已经发出去的 `stream` 事件（记录层是原始记录）——
    #    所以那半行会留在日志里，紧跟着一条 `retry` note 说它作废。
    try:
        async for mode, payload in graph.astream(state, config, stream_mode=["values", "messages"]):
            if mode == "messages":
                chunk, meta = payload
                # 🔴 **只发 agent 自己说的话**（`node='model'`）。
                #
                # `node='tools'` 是**工具内部的 LLM 调用** —— 实测就是试答那个
                # **结构化输出**（它跑在 `delegate` 里面，却被外层的 stream 捕获）。
                # 流出来的是**裸 JSON**：既不可读、又和**已经解开的展示重复** ——
                # 它们的结论在 `decision` 事件里已经是人话，这里再发一遍只是噪音。〔沿革 5〕
                if meta.get("langgraph_node") != "model":
                    continue
                # 🔴 **换了消息就先把缓冲区落盘**：模型调工具前会先说一句、工具跑完再接着
                #    说，**两段之间没有换行符** —— 攒在同一个 `buf` 里就被黏成一句。〔沿革 6〕
                #    判据用 `chunk.id`：同一条 AI 消息的流式 chunk 共享 id，换一次调用就换 id。
                # ⚠️ 有些 provider 不给 id（`None`）—— 那时**保持原行为**，不要瞎猜边界。
                cid = getattr(chunk, "id", None)
                if cid and cur_id and cid != cur_id:
                    flush_line(buf)
                    buf = ""
                if cid:
                    cur_id = cid

                text = _chunk_text(chunk)
                if not text:
                    continue
                buf += text
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    flush_line(line)
                continue
            yield payload
    finally:
        # 收尾：最后一行没有换行符。**异常时也走这里**（见上面那条 try/finally 的理由）——
        # 攒在 `buf` 里的那半行照发，读日志的人至少知道"它说到哪儿断的"。
        flush_line(buf)


def _chunk_text(chunk) -> str:
    """从一个流式 chunk 里取纯文本（内容可能是 str，也可能是分块列表）。"""
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(p.get("text") or "") if isinstance(p, dict) else str(p) for p in content
        )
    return ""
