"""**记录器** —— `logs/proto-*.log` 的**唯一落盘者**（一个**视图**，不是基础设施）。

## 这个文件是什么

把事件总线上流过的每一条事件**渲染成日志文件的一行（或一块）**，外加日志文件的
生命周期（在哪儿、什么时候建、什么时候关）。

```
业务代码 ──trace.*()──▶ 事件总线 _publish() ──┬──▶ **本模块**：落日志文件（权威记录）
                                            └──▶ 展示层（evals.chat 订阅）：自己排版上屏
```

## 设计原则（不变量）

1. **它是一个视图** —— 记录器和展示层是**同一类东西**：都是事件流的渲染，只不过一个
   落盘、一个上屏。所以它依赖 `handoff.decision`（决策块的**唯一那条渲染路**）
   **天经地义**：视图本来就该认识领域词汇表。
   🔴 它原来住在 `proto/trace.py` 里 —— 于是**把整条总线一起拖到了领域层上面**：
   `llm → trace → handoff.decision`，基础能力**传递依赖**领域词表，而 `handoff`
   也就永远抽不出去。〔沿革 1〕
2. **记录器不是"订阅者"** —— 总线**先调它**、再送订阅者：展示层坏了最多是"看不见"，
   而记录层丢一条 = **权威记录破了个洞**，所以它单独兜、兜住后把洞标出来。
   ⚠️ **这一点一个字没改**（`trace._publish` 里那两段原样）—— 变的只是"谁把它接上去"。
3. **日志是原始记录** —— 不截断、不收回已经流出去的字；要少看噪音就在**播报层**砍。
4. **落盘格式不是跨层契约** —— `_STREAM_MARK` / `_DECISION_HEAD` / `_NOTE_HEAD` 只是
   本模块自己的格式（而且名字私有）；展示层订阅事件、不读日志文件。
5. **惰性建文件** —— `LOG_FILE` 只是"文件会在哪儿"，真建出来是**第一次写**那一刻。

## 关键取舍

**① 为什么由装配处装，不是总线自己 import。** 总线 import 它 = 回到拆分前
（`llm → trace → handoff.decision`）。所以总线留一个**槽**（默认空），本模块在
**import 那一刻**自注册（同 `skills.py` 把内置表登记进 `registry` 那个模式）。
⇒ **代价如实说：忘了 import 本模块 = 日志静默没有。** 所以配了守卫
（`unit_decision` ⑮：三个入口都 import 了它）—— **接线靠守卫盯，不靠记性**。

**② 为什么"没装记录器"是合法状态。** 那是**库**的用法：宿主不想要日志文件就不 import
本模块 —— 总线照常发事件（展示层照样收），只是不落盘。

## 遗留问题

- **同步文件 IO 还在事件循环里**：`write()` 每行 `write + flush`、没有锁；
  `skill_pack` 的 `read_text`（3 处）、`artifacts.write_text` 同理。
  ⚠️ 异步迁移（`graph.py` 沿革 33）把节点与工具都改成 `async def`，**顺带取消了
  "同步阻塞只影响一个会话"那个前提** —— 现在它们卡的是**整个事件循环**。
  ⚠️ **量级没实测**：单会话交互式下 flush 是微秒级、无感；多会话高频流式下是不是真成
  瓶颈**不知道** —— 要实测再定级，别照抄外审给的 P2。

## 沿革（改过什么、为什么改）

> 编号是**引用锚**（正文里写 `〔沿革 N〕`）—— **只追加、不改号**，新条目加在**末尾**。

1. **建它**（2026-11-09）：从 `proto/trace.py` 拆出来。起因是量了一次依赖分层 ——
   `trace` 落在 **L2**（比 `llm` 和整个 `handoff/*` 都高），唯一原因是
   `trace → handoff.decision`（记录器要渲染决策块）。而记录器**是个视图**，
   视图依赖领域词汇表是对的 —— 错的是它和**总线**住在一个文件里。
   ⇒ 拆成三份：**总线**（`trace.py`，只依赖 `session`）／**本模块**（视图）／
   `decision()` 发射器归 `handoff.decision`（领域事件的形状归领域包）。
   🔴 **`_publish` 对记录器的处置一个字没改**（还是单独 try/except ＋ `_mark_hole`）——
   变的只是"谁把它接上去"：从硬接改成**槽位 ＋ 装配处安装**。
   ⚠️ **前史见 `trace.py` 沿革 1 / 17 / 19 / 30 / 31 / 32**（编号不动 —— 那是引用锚，
   而那些条目讲的东西现在住在这儿）。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from proto import session
from proto import trace
from proto.config import app_root
from proto.handoff.decision import render_decision  # 决策块的**唯一那条渲染路**

#: 日志落哪 —— 默认**应用根**下的 `logs/`（见 `proto.config.app_root`）：
#: 开发时是 `langgraph-lab/logs/`，打包后是 **exe 旁边的 `logs/`**。
#: ⚠️ 不能按 `__file__` 算：冻结后那是 `_internal/`，用户不该碰、重建还会被清掉。
#:
#: 🔴 **`logs/` 是给人看"真会话"的地方** —— 测试 / 跑批的日志是**噪音**，混进去会把
#: 翻会话日志的路堵死（实测：2309 个文件里 **826 个是噪音**：670 单测 ＋ 148 空 ＋ 8 探针）。
#: ⇒ 它们把 `PROTO_LOG_DIR` 指到 `tmp/logs/`（`tmp/` 本来就在 `.gitignore` 里）。
#: ⚠️ **由入口自己设**（16 个单测 ＋ `evals/run.py` 起子进程时），本模块只读这个开关 ——
#: 谁是噪音只有入口知道，这里猜不出来。〔`trace.py` 沿革 32〕
_log_dir = os.getenv("PROTO_LOG_DIR", "").strip()
LOG_DIR = Path(_log_dir) if _log_dir else app_root() / "logs"
# ⚠️ **相对路径相对"应用根"，不相对 cwd** —— 同 `config.app_root` 那条原则：
#    "从哪双击都不该影响读不读得到配置"。这里同理：从哪跑测试都不该把日志扔到别处。
if not LOG_DIR.is_absolute():
    LOG_DIR = app_root() / LOG_DIR
LOG_DIR.mkdir(parents=True, exist_ok=True)

#: 文件名后缀 —— 时间戳只到**秒**，跑批时多个子进程同秒启动会**撞同一个文件**。
#: `evals/` 的子进程用 `PROTO_LOG_TAG=<case>-r<rep>` 区分；手工跑时不设，行为不变。
_TAG = os.getenv("PROTO_LOG_TAG", "").strip()
_SUFFIX = f"-{_TAG}" if _TAG else ""

#: **默认会话**的日志文件 —— CLI / 跑批 / 单测都不开 `session()`，它们用的就是它。
#: ⚠️ 开了会话就**各有各的文件**（见 `log_file()`）。**日志格式一个字没改** ——
#: 这是选"每会话一个文件"（而不是"一个文件加会话列"）的全部理由：`record()` 的
#: "格式逐字节一致"那条约束**不用动**。〔`trace.py` 沿革 30〕
#: ⚠️ **它只是"文件会在哪儿"，不是"文件已经在了"** —— 真建出来是**第一次写日志**那一刻
#: （`_fh()` 惰性开）。原来这里就 `open("w")`：**`import` 这个动作本身**在磁盘上建文件，
#: 而大多数进程（单测、只用会话的宿主）一个字都不写它 —— 实测 `logs/` 里 **147 个空文件**。
#: 惰性之后**两条路一致**了（会话那条本来就是惰性的）。〔`trace.py` 沿革 31〕
LOG_FILE = LOG_DIR / f"proto-{time.strftime('%Y%m%d-%H%M%S')}{_SUFFIX}.log"
session.default().log_file = LOG_FILE
# ⚠️ **别在这儿 open** —— `log_fh = None` 就是"还没建"，`_fh()` 会替我们开。


def log_size() -> int:
    """当前会话的日志**已经写了多少字节** —— **文件还没建 = 0**。

    ⚠️ 为什么要它：惰性之后"这个进程还没写过日志"成了**正常状态**，
    直接 `LOG_FILE.stat()` 会 `FileNotFoundError`。读体积的地方一律走这儿。
    """
    p = log_file()
    return p.stat().st_size if p.exists() else 0


def log_file() -> Path:
    """**当前会话**的日志文件 —— 第一次写日志时才建（没写过日志的会话**不产生文件**）。

    ⚠️ 文件名里会话那一段**要消毒**：`thread_id` 由**宿主**给（web 那边可能就是用户 ID），
    直接拼进路径会被 `/`、`..` 带出去。非 `[A-Za-z0-9_-]` 一律换 `_`，再截断。
    """
    s = session.current()
    if s.log_file is None:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", s.thread_id)[:40] or "session"
        s.log_file = LOG_DIR / f"proto-{time.strftime('%Y%m%d-%H%M%S')}-{safe}{_SUFFIX}.log"
    return s.log_file


def _fh() -> Any:
    """当前会话**打开着的**日志句柄 —— 没开就开（跟着 `log_file()` 一起惰性建）。"""
    s = session.current()
    if s.log_fh is None:
        s.log_fh = log_file().open("w", encoding="utf-8")
    return s.log_fh


def write(line: str = "") -> None:
    """写一行到日志文件（不自动换行缩进，原样写）。**只属于记录器**，不发事件。

    ⚠️ **它只能被总线和本模块调**：别处直接调它就绕过了事件总线 ——
    展示层看不见那一行，而且是**静默**的。
    """
    _fh().write(line + "\n")
    _fh().flush()


def close() -> None:
    """关掉**当前会话**的日志句柄（没开过就什么都不做）。"""
    s = session.current()
    if s.log_fh is not None:
        s.log_fh.close()
        s.log_fh = None


BAR = "═" * 78


# ──────────────────────────── 记录器：唯一的文件落盘者 ────────────────────────────

#: 流式行在**日志文件**里的前缀 —— ⚠️ 只是记录器自己的落盘格式，**不是跨层契约**
#: （展示层订阅事件，不再读日志文件）。
#:
#: 🔴 **名字也私有**：公开名本身就是"可以被 import"的邀请 —— 下一个消费者写
#: `from proto.recorder import STREAM_MARK` 照样跑得通，"展示层认记录层的文本"那类事故
#: 就会原样复发。私有名一写出来，就是**伸手进记录层**的信号（白盒测试除外，
#: 它们本来就该盯文件格式）。〔`trace.py` 沿革 19〕
_STREAM_MARK = "⟨生成·"

#: 决策块的**首行前缀**（文件格式用）。同上：私有。
_DECISION_HEAD = "🧭 决策"

#: 某些 `kind` 的 note 在**文件里**的前缀 —— 文件格式的一部分，归记录器。
#:
#: 🔴 前缀原来是**业务侧**写进正文的（`f"finalize: 透传 …"`），展示层于是按**位置**
#: 切回来（`[len("finalize: "):]`）—— 只改文案措辞，上屏就被**静默吃掉 10 个字**（实测）。
#: 现在业务侧只发内容，前缀归记录层。〔`trace.py` 沿革 10〕
_NOTE_HEAD = {"relay": "finalize: "}

#: **一次 LLM 决策，人要看的四样** —— 顺序就是**复现这个决策**的顺序：
#:     前提（它看到了什么）→ 问题（它要回答什么）→ 结果（它答了什么）→ 下一步（平台据此干什么）
#:
#: ⚠️ **段名只有这一份**：记录器与展示层**都从这里取**（各自排版）—— 两边各抄一份的
#: 后果实测过：给这张表加一段，记录层整块决策从日志里**消失**、展示层**静默**少一段。
#: ⚠️ ~~`DECISION`~~ 已删 —— 它是**第二份段名表** ✗（`前提` / `问题` / `结果` / `下一步`，
#: **不带 `【】`**、缩进另算 4 格），把"段名一律 `【…】` 形式、独占行首"那条硬规矩
#: **开了第二个口子**。现在段名在 `SECTION`（`dec_given` / `dec_q` / `dec_result` /
#: 共用的 `next`），顺序在 `HOPS["decision"]`，渲染在 `handoff.decision.render_decision`。
#: 〔`trace.py` 沿革 26〕

#: 决策块的缩进 —— **记录层自己的排版**，与任何别层无关。
#: ⚠️ 它曾是 4，理由是"展示层要剥 2 格"—— 展示层改为订阅事件、自己排版后，这条跨层
#: 耦合**作废**；文件里留 4 格只是日志可读性的选择。〔`trace.py` 沿革 16〕
_DEC = "    "


def record(ev: dict) -> None:
    """把事件渲染成**日志文件的一行（或一块）**—— 文件格式的唯一出处。

    🔴 格式与事件源重构前**逐字节一致**（`unit_decision` / `unit_stream_join` 守着）：
    日志是权威记录，老日志、`_run.md` 附录、人的翻阅习惯都不断。〔`trace.py` 沿革 17〕

    ⚠️ 由**总线**调（`trace._publish` 里那一段），**不是订阅者** —— 见模块头原则 2。
    """
    ts = ev["ts"]
    t = ev["type"]
    if t == "section":
        write()
        write(f"{BAR}\n[{ts}] {ev['title']}\n{BAR}")
    elif t == "note":
        write(f"[{ts}] {_NOTE_HEAD.get(ev.get('kind') or '', '')}{ev['text']}")
    elif t == "block":
        write(f"[{ts}] {ev['label']}:")
        for line in str(ev["text"]).splitlines() or [""]:
            write(f"{ev['indent']}{line}")
    elif t == "stream":
        write(f"[{ts}] {_STREAM_MARK}{ev['who']}⟩ {ev['text']}")
    elif t == "handoff":
        write(f"[{ts}] 📤 交接 · {ev['hop']}（{ev['frm']} → {ev['to']}）：{len(ev['text'])} 字"
              f"｜结构化：{ev['structured'] or '（纯文本）'}")
        if ev["full"]:
            record({"type": "block", "label": f"交接全文 · {ev['hop']}",
                    "text": ev["text"], "indent": "  ", "ts": ts})
    elif t == "cut":
        write(f"[{ts}] ✂️ 截断 · {ev['where']}：保留 {ev['kept']} 字 / 原文 {ev['total']} 字"
              + (f"｜{ev['see']}" if ev.get("see") else ""))
    elif t == "dump":
        messages = ev["messages"]
        write(f"[{ts}] {ev['tag']} 消息轨迹（{len(messages)} 条）:")
        for i, m in enumerate(messages):
            write(f"  [{i}] {trace.render_message(m)}")
    elif t == "decision":
        # 🔴 **走唯一那条渲染路**：这里原来**自己排一遍**（缩进 4 格），而终端那边
        #    （`chat._render_decision`）**又排一遍**（0/2 格）—— 同一件事两处定义，
        #    形状必然漂。现在两边都调 `render_decision()`，**这里只加"文件格式"的东西**
        #    （时间戳前缀 + 段头）。〔`trace.py` 沿革 27〕
        body = "\n".join(f"{_DEC}{ln}" for ln in render_decision(ev).splitlines())
        write(f"[{ts}] {_DECISION_HEAD} · {ev['who']}\n{body}")


# 🔴 **装上** —— 总线留的是**槽**，这里就是那个装配处（**谁 import 本模块谁就装上**）。
#    ⚠️ 忘了 import = 日志静默没有 —— 这条接线由 `unit_decision` ⑮ 盯着三个入口，
#    **不靠记性**（见模块头「关键取舍 ①」）。
#    ⚠️ 记录器**不订阅**：总线直接先调它（顺序：记录器 → 订阅者）—— 这样它出错能走
#    `_mark_hole`（把洞标出来），而不是混进"订阅者出错"里。
trace.install_recorder(record=record, write=write)
