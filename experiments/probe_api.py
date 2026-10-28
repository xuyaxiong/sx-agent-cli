"""探测已安装版本里，离线文档提到的关键 API 是否可用、签名是否一致。

不依赖任何 API key：优先使用 fake chat model。
"""

from __future__ import annotations

import importlib
import inspect
import sys

# Windows 控制台默认 GBK，强制 UTF-8 避免中文乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def head(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def try_import(dotted: str) -> object | None:
    """按 'a.b.C' 逐级导入并返回最终对象。"""
    parts = dotted.split(".")
    for i in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
        except ImportError:
            continue
        for attr in parts[i:]:
            try:
                obj = getattr(obj, attr)
            except AttributeError:
                print(f"  [FAIL] {dotted}: 模块可导入但缺少属性 {attr!r}")
                return None
        print(f"  [OK]   {dotted}")
        return obj
    print(f"  [FAIL] {dotted}: 无法导入")
    return None


head("版本")
import langchain  # noqa: E402
import langchain_core  # noqa: E402
import langgraph  # noqa: E402
from importlib.metadata import version as _v  # noqa: E402

print(f"  python          {sys.version.split()[0]}")
for pkg in ("langgraph", "langchain", "langchain-core", "langgraph-checkpoint"):
    print(f"  {pkg:<16}{_v(pkg)}")

head("核心 API 导入")
create_agent = try_import("langchain.agents.create_agent")
try_import("langgraph.types.interrupt")
try_import("langgraph.types.Command")
try_import("langgraph.checkpoint.memory.InMemorySaver")
try_import("langgraph.checkpoint.memory.MemorySaver")
try_import("langgraph.graph.StateGraph")
try_import("langgraph.graph.MessagesState")
try_import("langgraph.graph.START")

head("fake model 候选（用于无 API key 的确定性测试）")
for name in (
    "GenericFakeChatModel",
    "FakeMessagesListChatModel",
    "FakeListChatModel",
    "FakeChatModel",
):
    try_import(f"langchain_core.language_models.fake_chat_models.{name}")

head("create_agent 签名")
if create_agent is not None:
    try:
        for name, p in inspect.signature(create_agent).parameters.items():
            print(f"  {name:<22} {p.kind.name:<10} default={p.default!r}")
    except Exception as e:  # noqa: BLE001
        print(f"  [FAIL] 无法读取签名: {e}")

head("compile / stream_events / interrupt 签名")
from langgraph.graph import START, MessagesState, StateGraph  # noqa: E402
from langgraph.types import interrupt  # noqa: E402

print(f"  StateGraph.compile.checkpointer = {inspect.signature(StateGraph.compile).parameters.get('checkpointer')}")

g = StateGraph(MessagesState).add_edge(START, "__end__").compile()
for attr in ("stream_events", "invoke", "stream", "get_state"):
    print(f"  {'[OK]  ' if hasattr(g, attr) else '[MISS]'} compiled.{attr}")
print(f"  stream_events: {inspect.signature(g.stream_events)}")
print(f"  interrupt    : {inspect.signature(interrupt)}")

print()
