"""**从同步代码跑协程** —— 一个进程**一个**事件循环。

## 这个文件是什么

产品是异步的（节点、工具、执行链全是 `async def`），但**宿主入口是同步的**：
CLI 的 `input()`、跑批的子进程、评分层。它们要驱动协程，就得有个同步收口 ——
**就是这一个**。

## 设计原则（不变量）

1. **一个进程一个循环**，不是"一次调用一个"。`asyncio.run()` 是"跑完就关循环" ——
   而 `config.get_model()` 是 `lru_cache` 的，里面那个 **async httpx 客户端绑在
   创建它的那个循环上**：换一个循环，第二轮就撞 `RuntimeError: Event loop is closed`。
2. **只有这一处** —— 每个入口各写一份 `_LOOP` 就是"同一个语义两处定义"，必然漂
   （实测：入口三处修好了，`judge` 那处漏了，同一场跑批里又报了一次）。
3. **它是给"进程就是一次会话"的宿主用的** —— web 那种一个进程多个会话的宿主
   自己在异步上下文里跑，**不该**走这儿。

## 沿革（改过什么、为什么改）

> 编号是**引用锚**（正文里写 `〔沿革 N〕`）—— **只追加、不改号**，新条目加在**末尾**。

1. **建它**（2026-11-09）。asyncio 迁移第 2 步（`d28b42b`）时，入口那三处各写了一份
   `_LOOP = asyncio.new_event_loop()` —— 修好了入口，**漏了 `evals/judge.py`**（它在
   评分层，每个 rubric 一次 `asyncio.run`）。跑批实测：`RuntimeError: Event loop is closed`
   → 第 1/2 次失败 → 靠重试蒙过去，蒙不过去就"评分调用失败，无法判定"。
   ⇒ 收成一处，四个调用方都走它。
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, TypeVar

_T = TypeVar("_T")

#: **进程级**那个循环 —— 见设计原则 1（为什么不能是 `asyncio.run`）。
_LOOP = asyncio.new_event_loop()


def run(coro: Coroutine[Any, Any, _T]) -> _T:
    """在**进程级**那个循环上把 `coro` 跑完，返回结果。

    ⚠️ **别在异步上下文里调它**（会报 "cannot be called from a running event loop"）——
    这个函数是给**同步宿主**用的收口，异步代码里直接 `await`。
    """
    return _LOOP.run_until_complete(coro)
