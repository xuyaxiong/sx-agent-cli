"""把原型跑的每一步写进文件，方便事后翻看「我问了 LLM 什么、它答了什么」。

日志落在**应用根**下的 `logs/proto-<时间戳>.log`（开发时是 `langgraph-lab/logs/`，
打包后是 exe 旁边的 `logs/`），同时打到控制台（简洁版）。
每次进程启动开一个新文件，不覆盖历史。

## 🔴 本模块同时是**事件源**（2026-11-03 架构定）

> "原则上显示层的逻辑和核心业务逻辑不该是同一个层级。"

所以本模块的公开函数（`section` / `note` / `block` / `handoff` / `decision` / `cut` /
`dump_trace` / `stream_agent`）**只发结构化事件**，不再直接写文件：

```
业务代码 ──trace.*()──▶ 事件总线 _publish() ──┬──▶ 记录器 _recorder：落日志文件（权威记录）
                                            └──▶ 展示层（evals.chat 订阅）：自己排版上屏
```

- **事件 = 记录层与展示层之间的唯一契约**。事件有两种标签，**两种都是受控词汇**
  （打错一个字母**当场炸**，不许静默跳过 —— `_check_role` / `_check_kind` / `_publish`）：
  - `type`：事件的**形状**（八种之一，见 `_EVENT_TYPES`）—— 决定它带哪些字段；
  - `kind`：业务**语义**（`dispatch` / `run_begin` / `relay` …，见 `KINDS`）——
    展示层据此决定归到哪个角色，**不再靠子串匹配认行**（措辞一改就静默失配的根）。
- **记录器不是"订阅者"**：`_publish` 先调它、再送订阅者。展示层坏了最多是"看不见"，
  而记录层丢一条 = **权威记录破了个洞** —— 所以它出错时**当场把洞标出来**（写明丢的是哪一条），
  不混在"订阅者出错"里（2026-11-03 审核 F4）。
- 展示层拿到的 `dump` 事件里是**消息对象本身**（`tool_call_id` 就在里面）——
  工具配对、来源标注全部走结构，旧的两条文本 hack（repr 反转义、按段 FIFO 配对）随之作废。
- 文件格式**保持不变**（`unit_decision` / `unit_stream_join` 守着）：
  日志是**原始记录**，供人与 `_run.md` 附录消费；记录层该怎么记还怎么记，
  只是**不再为展示层的排版让路**（旧 `_DEC=4` 那条耦合已废）。
- 展示层**不再读日志文件**：`_STREAM_MARK` 之类只是记录器自己的落盘格式，
  不是跨层契约（2026-11-03 定：契约是事件，不是文本）。
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any, Callable

from proto.config import app_root

#: 日志落**应用根**下的 `logs/`（见 `proto.config.app_root`）——
#: 开发时是 `langgraph-lab/logs/`，打包后是 **exe 旁边的 `logs/`**。
#: ⚠️ 不能按 `__file__` 算：冻结后那是 `_internal/`，用户不该碰、重建还会被清掉。
LOG_DIR = app_root() / "logs"
LOG_DIR.mkdir(exist_ok=True)

#: 文件名后缀 —— 时间戳只到**秒**，跑批时多个子进程同秒启动会**撞同一个文件**。
#: `evals/` 的子进程用 `PROTO_LOG_TAG=<case>-r<rep>` 区分；手工跑时不设，行为不变。
_TAG = os.getenv("PROTO_LOG_TAG", "").strip()
_SUFFIX = f"-{_TAG}" if _TAG else ""

LOG_FILE = LOG_DIR / f"proto-{time.strftime('%Y%m%d-%H%M%S')}{_SUFFIX}.log"
_fh = LOG_FILE.open("w", encoding="utf-8")

BAR = "═" * 78


def stamp() -> str:
    """日志时间戳：**年月日 + 毫秒**（用户 2026-10-30：*"日志实践精确到毫秒 添加上年月日信息"*）。

    以前只有 `HH:MM:SS` —— 一轮里同一秒内发生好几件事（工具调用 → 返回 → 判定），
    秒级看不出先后；跨天的日志也分不清是哪天。
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


# ──────────────────────────── 事件总线 ────────────────────────────

#: 事件的八种**形状**。每种带什么字段，见各 emitter 与 `_recorder`。
_EVENT_TYPES = (
    "section", "note", "block", "stream", "dump", "decision", "handoff", "cut",
)

_subs: list[Callable[[dict], None]] = []


def subscribe(fn: Callable[[dict], None]) -> None:
    """订阅事件总线。事件按发布顺序同步送达每个订阅者。

    🔴 订阅者抛异常**不会炸业务**：兜住并原样记进日志文件 ——
    展示层坏了最多是"看不见"，不能把正在跑的一轮弄死。
    """
    _subs.append(fn)


def _publish(ev: dict) -> None:
    """发一个事件：**先记录器**（唯一落盘者），再同步送每个订阅者（展示层）。

    🔴 记录器**不走订阅者那张网**（2026-11-03 审核 F4）：展示层坏了最多是"看不见"，
    记录层丢一条却是**权威记录破了个洞** —— 所以它单独兜，且兜住之后**把洞标出来**。
    """
    if ev.get("type") not in _EVENT_TYPES:
        # 形状只有那八种：打错一个字母，记录器与展示层都会**静默跳过**它 —— 宁可当场炸。
        raise ValueError(f"未知事件形状 type={ev.get('type')!r}：{list(_EVENT_TYPES)}")
    try:
        _recorder(ev)
    except Exception as e:  # noqa: BLE001 —— 不炸业务，但**必须把洞标出来**（见 _mark_hole）
        _mark_hole(ev, e)
    for fn in list(_subs):
        try:
            fn(ev)
        except Exception as e:  # noqa: BLE001 —— 见 subscribe 的 docstring
            _fh.write(
                f"[{stamp()}] ⚠️ 事件订阅者出错"
                f"（{getattr(fn, '__qualname__', fn)}）：{type(e).__name__}: {e}\n"
            )
            _fh.flush()


def _mark_hole(ev: dict, e: Exception) -> None:
    """记录器渲染失败 → 在日志里**把洞标出来**（交接三问②：切了必须标）。

    ⚠️ 标洞本身也兜住（比如文件已关）—— 但**不许静默**：正常路径下这行会写进日志，
    读的人一眼知道**丢的是哪一条**，而不是"日志里怎么少了这一段"。
    """
    try:
        _write(
            f"[{stamp()}] ⚠️ **这一条没有落进日志**（记录器渲染失败）："
            f"type={ev.get('type')!r} kind={ev.get('kind')!r}"
            f" —— {type(e).__name__}: {e}"
        )
    except Exception:  # noqa: BLE001 —— 连标洞都写不进去，只能放弃；仍不炸业务
        pass


def _write(line: str = "") -> None:
    """写一行到日志文件（不自动换行缩进，原样写）。**只属于记录器**，不发事件。

    ⚠️ 私有：别处直接调它就绕过了事件总线 —— 展示层看不见那一行，而且是**静默**的。
    """
    _fh.write(line + "\n")
    _fh.flush()


# ──────────────────────────── 事件发射器（业务代码只调这一层） ────────────────────────────

#: **角色枚举** —— 展示层按它归版式（谁说的话、什么颜色），**不从 `who` 的措辞里猜**。
#:
#: 🔴 2026-11-03 e2e 实测暴露的残余耦合：`chat.py` 原来靠
#: `"执行器" in ev["who"]` / `"run_task" in ev["tag"]` 认角色 —— 而 `who`/`tag`
#: 是**给人看的自由文本**（`执行器·lesson-prep`、`run_task 第 1 轮`），
#: 哪天改个措辞，展示层就**静默降级**成主 agent 的角色，没有任何报错。
#: 这正是原型自己反对的那种失配（`handoff/__init__` 那条"禁止反结构化"铁律），
#: 只是从"日志行"挪到了"字段里"。
#:
#: 现在拆成两样：**`role` 是逻辑（枚举，逻辑依赖它）**、**`who` 是显示名（随便改）**。
#: 加新角色就加在这里 —— 加完 `ROLES` 会自动收紧，漏传的地方当场报错。
AGENT = "agent"
EXECUTOR = "executor"
ROLES = frozenset({AGENT, EXECUTOR})


def _check_role(role: Any, where: str) -> str:
    """角色只能是枚举值 —— 传错就**当场炸**，不许静默当成主 agent。"""
    if role not in ROLES:
        raise ValueError(
            f"{where} 的 role 必须是 {sorted(ROLES)} 之一，收到 {role!r}。"
            "（角色是逻辑，不是措辞：见 trace.AGENT / trace.EXECUTOR）"
        )
    return role


#: **kind 的受控词汇** —— 业务语义标签，展示层按它归角色 / 归并。
#:
#: 🔴 2026-11-03 审核 F3：kind 原来是**自由字符串** —— 把 `dispatch` 打成 `delegate`、
#: 或改个名，终端上那一行就**静默消失**，没有任何测试会红。这和这次修掉的失配是
#: **同一类**（只是从"措辞"挪到了"kind 字符串"），所以按 `role` 的办法收口：
#: 加一个 kind 就登记在这里，漏登记的地方**当场炸**。
#: ⚠️ 加完还要去 `evals/chat.py` **表态**（上屏 / 有专门分支 / 只进日志）——
#: 漏表态 `unit_console` 会红。
KINDS = frozenset({
    # agent.delegate
    "dispatch", "supersede", "waiting", "delegate_failed",
    # executor.run_task
    "run_begin", "run_end", "executor_briefing", "nudge",
    # graph.prepare / finalize
    "feed", "relay",
    # llm.safe_structured（`llm_io` = 那次调用的 PROMPT / RESPONSE / 失败提示）
    "ask_main", "judge", "llm_io",
    # trace.stream_agent（`retry` = 这一轮抖动、已重试一次）
    "retry",
    # executor.run_task（`run_fault` = 重试过仍失败，这一轮没跑成）
    "run_fault",
})


def _check_kind(kind: str, where: str) -> str:
    """kind 只能是受控词汇里的值（空串 = 纯记录、不上屏）—— 打错就**当场炸**。"""
    if kind and kind not in KINDS:
        raise ValueError(
            f"{where} 的 kind={kind!r} 不在受控词汇里（{sorted(KINDS)}）。"
            "加新 kind 要同时：① 登记进 trace.KINDS；"
            "② 在 evals/chat.py 表态（上屏 / 有专门分支 / 只进日志）。"
        )
    return kind


def section(title: str, kind: str = "") -> None:
    """一个**段落头**（前后两道 `═══` 包着标题）。

    `kind`：业务语义标签（受控词汇，见 `KINDS`）—— 展示层按它归角色；
    不给就是"纯记录，不上屏"。
    """
    _publish({"type": "section", "kind": _check_kind(kind, "section"),
              "title": title, "ts": stamp()})


def note(text: str, kind: str = "") -> None:
    """一行记录（带时间戳）。`kind` 的语义同 `section`。

    ⚠️ **只发内容**：这一行在日志文件里的前缀（如 `relay` 的 `finalize: `）归**记录器**
    —— 业务侧带上它，展示层就得按位置切回来（2026-11-03 审核 F1 实测的那种失配）。
    """
    _publish({"type": "note", "kind": _check_kind(kind, "note"), "text": text, "ts": stamp()})


def block(label: str, text: str, *, kind: str = "", indent: str = "  ") -> None:
    """一个带标签的多行文本块（prompt / response / 交接正文）。"""
    _publish({"type": "block", "kind": _check_kind(kind, "block"), "label": label,
              "text": text, "indent": indent, "ts": stamp()})


def kv(label: str, **fields: Any) -> None:
    """写一行键值对（形状上是一行 note）。"""
    inner = " | ".join(f"{k}={v}" for k, v in fields.items())
    _publish({"type": "note", "kind": "", "text": f"{label} | {inner}", "ts": stamp()})


def handoff(
    hop: str,
    frm: str,
    to: str,
    text: str,
    *,
    structured: str = "",
    full: bool = False,
) -> None:
    """**一次交接的完整记录** —— 一行摘要（＋可选全文）。

    用户 2026-11-03：*"你得加上关键日志 —— 这些在日志中得完整记录下来**便于复盘**。"*

    摘要行让复盘的人**一眼看到**：
      - **谁给谁、交了多少字** → 判据①（内容）
      - **有没有结构化对象** → 判据④（结构 ＋ 模板两层分离）
      - **跳名与方向** → 判据⑤（这一跳是平台硬拼还是 LLM 转述）

    截断（判据②）由**切的那一方**自己报（`cut()`）—— 不在这里事后解析文本。
    膨胀（判据③）看每轮的上下文读数（`harness.snapshot` / CLI 的 `📊 CONTEXT` 行）。

    ⚠️ `full=True` 只在**别处没有 dump 过**这一跳时用（否则日志里两份）。
    """
    _publish({"type": "handoff", "kind": "", "hop": hop, "frm": frm, "to": to,
              "text": text, "structured": structured, "full": full, "ts": stamp()})


def decision(
    who: str,
    *,
    q: str = "",
    given: Any = (),
    result: Any = (),
    nxt: str = "",
) -> None:
    """记一次 **LLM 决策**：前提 → 问题 → 结果 → 下一步（段名见 `DECISION`）。

    🔴 发的是**结构化事件**（字段原样在 `ev` 里），渲染成什么样是**记录器与展示层各自的事**
    —— 展示层不再从渲染文本里反解析（2026-11-03 定）。

    `given` / `result` 传**列表**（逐项一行）→ 渲染成 `- ` 列表；传字符串 → 一行。
    ⚠️ 列表化不是装饰：判定那段原来直接把 `j.missing` 打出来，人看到的是
    **Python repr** `['尚未收到…', '教案文本文件产物为空…']` —— 括号引号全是噪音。
    """
    _publish({"type": "decision", "kind": "", "who": who, "q": q,
              "given": given, "result": result, "next": nxt, "ts": stamp()})


def cut(where: str, kept: int, total: int, *, see: str = "") -> None:
    """**切了一刀就报一刀** —— 由切的那一方自己调，不靠事后从文本里解析。

    用户 2026-11-03：*"主动截断是否有显式标记，被截断的内容，有没有告知它能查到
    全文详情的方式方法。"* —— 标记在**文本里**（读的人看得见），
    这一行在**日志里**（复盘的人看得见），两者都要有。

    - `where`：**切的是哪一份**，要说人话（谁看到的东西被切了、原件动没动）。
    - `kept` / `total`：**实测**字数（不是预算）—— 报意图不报事实，改天就静默不符。
    - `see`：**补一句给读日志的人**（"全文在哪"／"为什么没有全文"）—— 原样接在 `｜` 后面，
      所以措辞由调用点负责（要写"全文：…"就自己写）。
    """
    _publish({"type": "cut", "kind": "", "where": where, "kept": kept,
              "total": total, "see": see, "ts": stamp()})


def dump_trace(tag: str, messages: list, *, role: str) -> None:
    """把一整条消息轨迹**作为事件**发出去（执行器内部发生了什么，全在这）。

    🔴 事件里带的是**消息对象本身**（不是渲染好的行）—— 展示层靠 `tool_call_id`
    做工具配对、靠 `additional_kwargs` 认平台注入，**不从渲染文本里反推结构**。

    `role`：说话的是谁（`trace.AGENT` / `trace.EXECUTOR`），**必填** ——
    展示层按它归版式。`tag` 只是给人看的描述（`run_task 第 1 轮`），
    **逻辑不许从 tag 里认东西**（2026-11-03 e2e 实测踩过：`"run_task" in tag`）。
    """
    _check_role(role, "dump_trace")
    _publish({"type": "dump", "kind": "", "role": role, "tag": tag,
              "messages": list(messages), "ts": stamp()})


# ──────────────────────────── 消息渲染（记录器专用） ────────────────────────────

#: **正文类参数**：只留字数。
#:
#: 🔴 **只有它们该省**（2026-10-30 用户纠正）：`write_artifact(content=...)` 的正文
#: **另有出处**（产物文件），灌进日志纯属浪费。
#:
#: ⚠️ **其余参数一律照原样给全** —— 原来按"超过 120 字就省"一刀切，
#: 结果把 `delegate(task=...)` 也省成了 `<220字>`，而**那正是整个交接的核心**
#: （用户：*"你怎么把这么关键的上下文交接信息给去掉了？？"）。
#: 判据跟显示层那条一样：**全文另有出处才省，没有就全给**。
BODY_ARGS = frozenset({"content"})


def brief_args(args: Any, limit: int = 120) -> str:
    """工具参数的一行摘要 —— **只有 `BODY_ARGS` 里的正文类参数**会被压成字数。"""
    if not isinstance(args, dict):
        return str(args)
    parts = []
    for k, v in args.items():
        text = str(v)
        parts.append(f"{k}=<{len(text)}字>" if k in BODY_ARGS and len(text) > limit else f"{k}={text!r}")
    return ", ".join(parts)


def tool_calls_of(messages: list) -> list[tuple[str, str]]:
    """从消息里抽出**全部**工具调用 —— `(名字, 参数摘要)`，按发生顺序。

    主 agent 的 `read_artifact` 这类调用，原来在日志里查不到（只有执行器的轨迹被 dump），
    只能靠"消息类型对不上"反推。这个函数是给日志补上那一段的。
    """
    out: list[tuple[str, str]] = []
    for m in messages:
        for c in getattr(m, "tool_calls", None) or []:
            out.append((str(c.get("name")), brief_args(c.get("args") or {})))
    return out


def render_message(m) -> str:
    """把一条 LangChain 消息渲染成可读文本（**记录器落盘用**）。

    🔴 **这里不截断**（2026-10-30 用户定）：日志是**原始记录**，
    而"平台注入了什么、agent 说了什么"正是**优化 agent 表现的唯一观察窗口** ——
    *"这是优化agent表现的细节 如果没有这些东西观察不到"*。
    要少看噪音就在**播报层**砍，别在记录层砍。

    ⚠️ **AI 的话只写头部，不重复正文** —— 正文已经由 `stream_agent` 逐行流过日志了。
    """
    kind = type(m).__name__
    content = str(m.content)
    parts = [f"{kind}({len(content)}字)"]
    # 🔴 **平台注入的标记**（`proto.handoff.INJECTED`）—— 透传注入的是 AIMessage，
    # 内容是**执行器的原话**；不写出来，读日志的人会以为那是主 agent 说的。
    tag = (getattr(m, "additional_kwargs", None) or {}).get("injected_by")
    if tag:
        parts.append(f"    injected_by={tag}")
    if content.strip() and kind != "AIMessage":
        # `!r` 只为**压成一行**（日志一行一条），不是为了截断。
        parts.append(f"    content={content!r}")
    for c in getattr(m, "tool_calls", None) or []:
        parts.append(f"    tool_call={c.get('name')} args={brief_args(c.get('args') or {})}")
    return "\n".join(parts)


def stream_agent(graph, state, config, who: str, *, role: str):
    """跑一个 agent 图，**每一步 + 每个字**都录下来 —— 边跑边发 `stream` 事件。

    Yields：每个 super-step 的完整 state（`stream_mode="values"` 那一支）。

    🔴 **为什么要有它**（2026-10-30）：`invoke` 会把整个 agent 循环跑完才返回，
    于是"模型→工具→模型→工具…"这一串**全程静默**。实测一次真实会话：
    主 agent 连调 8 个工具、**336 秒里日志一行不动** —— 而用户侧就是**黑屏**，
    分不清"在干活"还是"卡住了"。

    两层一起给：
      - **每个 super-step** → `values` 那一支，调用方拿去做逐步骤 dump
      - **每个字** → `stream` 事件（攒到换行发一行），记录器落盘、展示层实时上屏

    ⚠️ **前提：所有 `AIMessage` 都从这里流过** —— 所以记录器对 AI 的话
    **只写头部、不再重复正文**（正文已经在上面流出来了）。改调用点时记得这条。

    ⚠️ 多个执行器**并行**时，它们的事件会交错 —— 所以 `who` 要能分辨是谁
    （主 agent / 执行器·`<技能名>`）。

    ⚠️ **只发 `node='model'` 的 chunk** —— 工具内部的结构化调用（判定 / 试答）
    流出来是裸 JSON，既不可读、又和已解开的展示重复。

    `role`：`trace.AGENT` / `trace.EXECUTOR`，**必填** —— 展示层按它归版式。
    `who` 只是显示名（`执行器·lesson-prep`），**随便改，不影响任何逻辑**。
    """
    # 🔴 校验在**生成器外面**：漏传 / 传错立刻炸，而不是等第一次 next()。
    return _stream_with_retry(graph, state, config, who, _check_role(role, "stream_agent"))


def _stream_with_retry(graph, state, config, who: str, role: str):
    """跑 `_stream_agent`；**基础设施抖动就重试一次**，两次都失败才抛。

    🔴 **为什么兜在这一层**（2026-11-04 复审 N2/R8）：
      - 这是**两条链唯一共用的缝** —— 主 agent 的循环、执行器的循环都从这过。
        放这儿，两边一起有兜底，不会"改了一条忘了另一条"（那正是 R8 的形状：
        `executor.py` / `agent.py` 全文一个 try/except 都没有）。
      - **降级不在这里**：两条链该怎么退化不一样（执行器 → 落"这轮没跑成"交主 agent 决定；
        主 agent → 让**会话不死**，用户再说一遍就行）。这里只做最保守的一步：
        重试一次，然后把异常**原样抛给调用方**。
      - 形状同 `llm.safe_structured`（重试一次 → 仍失败才退化）—— **先例在，别另立一套**。

    ⚠️ 重试是**整轮重跑**（`graph.stream` 从头来）：两条链的内层 agent 都**没有 checkpointer**
      （`InMemorySaver` 在外层主图上），所以重跑 = 干净重来，不会把消息叠两遍。
    ⚠️ 已经流出去的字**不收回**（记录层是原始记录）—— 所以重试前先发一条 `retry` 事件，
      把"上面那半段作废、现在重来"说破；不然读日志的人会以为模型把话说重了。
    """
    for attempt in (1, 2):
        try:
            yield from _stream_agent(graph, state, config, who, role)
            return
        except Exception as e:  # noqa: BLE001 —— 抖动的种类不可穷举（网络/超时/限流/网关）
            if attempt == 2:
                raise
            _publish({
                "type": "note", "kind": "retry", "ts": stamp(),
                "text": f"⚠️ {who} 这一轮**基础设施抖动**（{type(e).__name__}: {e}）"
                        "—— 已重试一次（上面流出去的那半段作废）",
            })


def _stream_agent(graph, state, config, who: str, role: str):
    buf = ""
    cur_id: str | None = None      # 当前这条 AI 消息的 id —— 换了消息就把 buf 落盘

    def flush_line(line: str) -> None:
        if line.strip():
            _publish({"type": "stream", "kind": "", "role": role, "who": who,
                      "text": line, "ts": stamp()})

    for mode, payload in graph.stream(state, config, stream_mode=["values", "messages"]):
        if mode == "messages":
            chunk, meta = payload
            # 🔴 **只发 agent 自己说的话**（`node='model'`）。
            #
            # `node='tools'` 是**工具内部的 LLM 调用** —— 实测就是判定 / 试答那几个
            # **结构化输出**（它们跑在 `delegate` 里面，却被外层的 stream 捕获）。
            # 流出来的是**裸 JSON**：既不可读（用户 2026-10-30：*"就打个 json 算啥"*），
            # 又和**已经解开的展示重复**。所以这里跳过：它们的结论在
            # `decision` 事件里已经是人话，这里再发一遍只是噪音。
            if meta.get("langgraph_node") != "model":
                continue
            # 🔴 **换了消息就先把缓冲区落盘**（2026-11-02 实测踩到）。
            #
            # 模型调工具前会先说一句、工具跑完再接着说，**两段之间没有换行符** ——
            # 攒在同一个 `buf` 里就被黏成一句。判据用 `chunk.id`：
            # 同一条 AI 消息的流式 chunk 共享 id，换一次模型调用就换 id。
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
    flush_line(buf)  # 收尾：最后一行没有换行符


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


def close() -> None:
    _fh.close()


# ──────────────────────────── 记录器：唯一的文件落盘者 ────────────────────────────

#: 流式行在**日志文件**里的前缀 —— ⚠️ 只是记录器自己的落盘格式，**不是跨层契约**
#: （展示层订阅事件，不再读日志文件；2026-11-03 定）。
#:
#: 🔴 **名字也私有**（2026-11-04 复审 N1）：公开名本身就是"可以被 import"的邀请 ——
#: 下一个消费者写 `from proto.trace import STREAM_MARK` 照样跑得通，
#: "展示层认记录层的文本"那类事故就会原样复发。私有名一写出来，
#: 就是**伸手进记录层**的信号（白盒测试除外，它们本来就该盯文件格式）。
_STREAM_MARK = "⟨生成·"

#: 决策块的**首行前缀**（文件格式用）。同上：私有。
_DECISION_HEAD = "🧭 决策"

#: 某些 `kind` 的 note 在**文件里**的前缀 —— 文件格式的一部分，归记录器。
#:
#: 🔴 2026-11-03 审核 F1：前缀原来是**业务侧**写进正文的（`f"finalize: 透传 …"`），
#: 展示层于是按**位置**切回来（`[len("finalize: "):]`）—— 只改文案措辞，上屏就被
#: **静默吃掉 10 个字**（实测）。现在业务侧只发内容，前缀归记录层。
_NOTE_HEAD = {"relay": "finalize: "}

#: **一次 LLM 决策，人要看的四样** —— 顺序就是**复现这个决策**的顺序：
#:     前提（它看到了什么）→ 问题（它要回答什么）→ 结果（它答了什么）→ 下一步（平台据此干什么）
#:
#: 🔴 用户 2026-11-03：*"decide 那一段日志也得整理下格式，人看着也不清晰。既然是决策，
#: 很显然：**决策前提是什么、要决策的问题是什么、决策结果是什么**，有可能还要补充
#: **下一步该干什么**。"*
#:
#: ⚠️ **段名只有这一份**：记录器与展示层**都从这里取**（各自排版）。
#: 两边各抄一份的后果实测过（2026-11-03 审核 F2）：给这张表加一段，
#: 记录层整块决策从日志里**消失**、展示层**静默**少一段。
DECISION: dict[str, str] = {
    "given": "前提",
    "q": "问题",
    "result": "结果",
    "next": "下一步",
}

#: 决策块的缩进 —— **记录层自己的排版**。（⚠️ 2026-11-03 前它是 4，理由是"展示层要剥
#: 2 格"—— 展示层已改为订阅事件、自己排版，这条跨层耦合**作废**；文件里留 4 格
#: 只是日志可读性的选择，与任何别层无关。）
_DEC = "    "


def _recorder(ev: dict) -> None:
    """把事件渲染成**日志文件的一行（或一块）**—— 文件格式的唯一出处。

    🔴 格式与 2026-11-03 重构前**逐字节一致**（`unit_decision` / `unit_stream_join` 守着）：
    日志是权威记录，老日志、`_run.md` 附录、人的翻阅习惯都不断。
    """
    ts = ev["ts"]
    t = ev["type"]
    if t == "section":
        _write()
        _write(f"{BAR}\n[{ts}] {ev['title']}\n{BAR}")
    elif t == "note":
        _write(f"[{ts}] {_NOTE_HEAD.get(ev.get('kind') or '', '')}{ev['text']}")
    elif t == "block":
        _write(f"[{ts}] {ev['label']}:")
        for line in str(ev["text"]).splitlines() or [""]:
            _write(f"{ev['indent']}{line}")
    elif t == "stream":
        _write(f"[{ts}] {_STREAM_MARK}{ev['who']}⟩ {ev['text']}")
    elif t == "handoff":
        _write(f"[{ts}] 📤 交接 · {ev['hop']}（{ev['frm']} → {ev['to']}）：{len(ev['text'])} 字"
               f"｜结构化：{ev['structured'] or '（纯文本）'}")
        if ev["full"]:
            _recorder({"type": "block", "label": f"交接全文 · {ev['hop']}",
                       "text": ev["text"], "indent": "  ", "ts": ts})
    elif t == "cut":
        _write(f"[{ts}] ✂️ 截断 · {ev['where']}：保留 {ev['kept']} 字 / 原文 {ev['total']} 字"
               + (f"｜{ev['see']}" if ev.get("see") else ""))
    elif t == "dump":
        messages = ev["messages"]
        _write(f"[{ts}] {ev['tag']} 消息轨迹（{len(messages)} 条）:")
        for i, m in enumerate(messages):
            _write(f"  [{i}] {render_message(m)}")
    elif t == "decision":
        out = [f"{_DECISION_HEAD} · {ev['who']}"]
        # 🔴 键表**从 `DECISION` 取**，不在这里再抄一份（抄一份的后果见 `DECISION` 的注释）。
        vals = {k: ev.get(k) for k in DECISION}
        for key in DECISION:
            val = vals[key]
            items = list(val) if isinstance(val, (list, tuple)) else [val]
            items = [str(x) for x in items if str(x or "").strip()]
            if not items:
                continue
            if len(items) == 1 and not isinstance(val, (list, tuple)):
                ls = items[0].splitlines() or [""]
                out.append(f"{_DEC}{DECISION[key]}：{ls[0]}")
                out += [f"{_DEC}  {ln}" for ln in ls[1:]]
            else:
                out.append(f"{_DEC}{DECISION[key]}：")
                for it in items:
                    ls = it.splitlines() or [""]
                    out.append(f"{_DEC}  - {ls[0]}")
                    out += [f"{_DEC}    {ln}" for ln in ls[1:]]
        _write(f"[{ts}] " + "\n".join(out))


# ⚠️ 记录器**不订阅**：`_publish` 直接先调它（顺序：记录器 → 订阅者）——
# 这样它出错能走 `_mark_hole`（把洞标出来），而不是混进"订阅者出错"里（审核 F4）。
