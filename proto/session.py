"""**会话**（Session）：一次会话的**全部运行期状态** —— 多会话共享进程时，按它隔离。

## 这个文件是什么

`proto` 原来把运行期状态挂在**模块级**（`artifacts._ARTIFACTS` / `trace._subs` /
`trace._fh`…），那是"**一个进程一个会话**"的形状。要包成**库**、接**多用户 web** 时，
这个形状就串了 —— 实测最重的一处：`artifacts.list_artifacts` 的 docstring 里写着
"**本会话**"，而它读的是**进程级**那张表，于是用户 B 会看到用户 A 的全部产物。

本模块给出那个**隔离单位**：`Session` 装状态、`current()` 取"当前会话"、
`session(thread_id)` 开一个（**这就是库的对外 API**）。

## 设计原则（不变量）

1. **对外显式、对内隐式** —— 库的 API 是一个**会话对象**（顶层看得见）；
   内部传播走 **contextvar**，所以 105 个 `trace.xxx(...)` / 那 8 个 `artifacts.xxx(...)`
   调用点**一个不改**。
2. **没设 context 就回落默认会话** —— CLI、跑批、16 个单测的行为**一个字不变**。
   ⚠️ 这是"能一个模块一个模块搬"的前提：**中间态永远跑得起来**。
3. **会话是运行期上下文，不是领域概念** —— `handoff/` 那些纯渲染函数**不许**知道它。
   所以**不走**"每会话一个对象、一路传参"（那会把会话塞进领域层）。
4. **要隔离的状态只许登记在这里**，不许再挂模块级 —— 加一样就在 `Session` 上加一个字段。

## 关键取舍

**① 为什么是 contextvar，不是一路传参。** 传参要改 **105 个调用点 / 17 个文件**，
而且每个业务函数都要多一个参数 —— 那正是原则 3 说的分层污染。contextvar 在
**async web 框架里天然对齐**（每个请求一个 context），"当前会话是谁"框架已经替我们管了。

**② 回落默认会话会不会藏出一条分叉。** 不会：它**只有一处定义**（`_DEFAULT`），
而且**只有上下文里没设时**才取得到 —— 生产路径（web）永远在 `session()` 里跑。

## 遗留问题

- **`trace` 的两样还没搬进来**（`_subs` 订阅者 / `_fh` 日志句柄）：下一轮。搬完顺带
  把 `thread_id` 带进事件、日志行带会话列、`subscribe()` 返回句柄（可退订）。
- **默认会话是进程级的、永不回收**。单会话宿主无所谓；多会话宿主里它应当**取不到**
  （或当场炸），而不是悄悄接住 —— 等真有第二个宿主时再定。

## 沿革（改过什么、为什么改）

> 编号是**引用锚**（正文里写 `〔沿革 N〕`）—— **只追加、不改号**，新条目加在**末尾**。

1. **建它**（2026-11-06）。起因：用户定 —— *"我这个会封装成一个库 面向多用户
   所以所有东西都应该是面向 session 级的"*。全仓扫了一遍模块级可变状态（**28 处**）：
   23 处**从没被写**（`SECTION` / `HOPS` / `FAULT_MARK`… 词汇表和常量表，共享没问题），
   **5 处会被写** —— 其中真正**串数据**的只有**产物库**（`_ARTIFACTS` ＋ `_ART_SEQ` ＋
   `ARTIFACT_DIR`）。⇒ 先搬它，`trace` 的两样下一轮。
   ⚠️ `unit_artifact_race` ② 那条守卫的注释里**早就写着这个场景**（*"换真存储
   （表 / **按会话分库**）"*）—— 当初把表私有化、对外只留函数，正是为了今天只动
   `artifacts.py` **一个文件**。
"""

from __future__ import annotations

import itertools
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator


class Session:
    """一次会话的**全部运行期状态** —— 字段按**归属的模块**分组。

    ⚠️ 加字段的规矩（设计原则 4）：**要隔离的状态只许加在这儿**，不许再挂模块级。
    """

    def __init__(self, thread_id: str = "") -> None:
        #: 这次会话的标识 —— 与 LangGraph 的 `config["configurable"]["thread_id"]` **同一个**。
        self.thread_id = thread_id
        # ── 产物库（`proto/artifacts.py` 用）──
        #: 产物记录：`aid → {title, kind, file_type, content, file, path}`。
        #: ⚠️ **对外只有函数，不给这个 dict**（`artifacts` 模块头原则 1）。
        self.artifacts: dict[str, dict[str, str]] = {}
        #: 产物 ID 发号器 —— **每会话一条序列**（`itertools.count()`：`next()` 原子，
        #: 不用 `len(表) + 1` 那种"先查后写"）。〔`artifacts` 沿革 3〕
        self.artifact_seq: Iterator[int] = itertools.count(1)
        #: 产物落盘到哪。`None` = 只进内存（跑批那条路径就是这样）。
        self.artifact_dir: Path | None = None


#: 默认会话 —— **只有上下文里没设时才取得到**（见「关键取舍 ②」）。
_DEFAULT = Session(thread_id="")

_current: ContextVar[Session | None] = ContextVar("proto_session", default=None)


def current() -> Session:
    """**当前会话** —— 没设就回落到那个默认会话。

    ⚠️ 回落是**故意的**（设计原则 2）：CLI / 跑批 / 单测都不设上下文，靠它保持原行为。
    """
    return _current.get() or _DEFAULT


@contextmanager
def session(thread_id: str = "") -> Iterator[Session]:
    """开一个**会话**（库的对外 API —— web 每个请求一层）。

        with proto.session(thread_id=user_id) as s:
            ...            # 这段里所有 artifacts / trace 调用都归这个会话
    """
    s = Session(thread_id=thread_id)
    token = _current.set(s)
    try:
        yield s
    finally:
        _current.reset(token)
