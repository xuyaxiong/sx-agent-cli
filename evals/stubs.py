"""**测试专用**的注入桩 —— 跑批 case 里那两个 `fault.*` 旋钮的实现。

## 这个文件是什么

让 `safe_structured` 内部那一步先失败 N 次（`fault.structured`）、或让 `stream_agent`
前 N 次调用**直接抛**（`fault.stream`）—— 用来验**降级**那条路真的走了。

## 设计原则（不变量）

1. **必须在 `install()` 之后调** —— 它包的是 `install()` **装好的那一层**：
   `proto.llm.get_model` 那时已经是 `counting`、`proto.stream.stream_agent` 是原函数。
2. **它往 `inst` 里那两个 dict 写**（`fault` / `stream_fault`），**不是自己造一份** ——
   运行记录里带着它们，**产品那条路也要写出这个键**，所以形状由 `app/runtime.py`
   定义（空 dict），这里只填。⚠️ 是**同一个对象**，`install()` 返回的就是它。
3. **`role` 用枚举过滤，不按 `who` 的措辞** —— `who` 是显示名（"执行器·lesson-prep"），
   拿它做判据正是这套代码一直在治的病（`trace.AGENT` / `trace.EXECUTOR`）。

## 沿革（改过什么、为什么改）

> 编号是**引用锚**（正文里写 `〔沿革 N〕`）—— **只追加、不改号**，新条目加在**末尾**。

1. **建它**（2026-11-09）：从 `app/runtime.py` 的 `install()` 里搬出来。
   它是**纯测试旋钮** —— `app/chat.py`（产品入口）**根本没有** `--fault` 这个开关，
   而它原来和产品装配住在同一个函数里，就是"测试关切混进产品模块"。
   见 `app/runtime.py` 沿革 2。
"""

from __future__ import annotations

import proto.llm
import proto.stream
import proto.trace


class _FlakyStructured:
    """让 `with_structured_output(...).invoke()` 先失败 N 次。

    ⚠️ 注入点在 `safe_structured` **内部那一步**（`get_model().with_structured_output(...)`），
    这样测的才是 `safe_structured` **自己的重试 + 保守默认值**兜底，
    而不是绕过它。`proto.llm.get_model` 是值导入，patch 这里正好。
    """

    def __init__(self, inner, state: dict) -> None:
        self._inner = inner
        self._state = state

    def __getattr__(self, k):  # noqa: ANN001
        return getattr(self._inner, k)

    def with_structured_output(self, schema, **kw):  # noqa: ANN001
        inner = self._inner.with_structured_output(schema, **kw)
        state = self._state

        class _R:
            def invoke(self, prompt, **kw2):  # noqa: ANN001
                if state["left"] > 0:
                    state["left"] -= 1
                    state["fired"] += 1
                    raise RuntimeError("注入的抖动：sandbox grammar compile timed out")
                return inner.invoke(prompt, **kw2)

        return _R()


def inject_faults(case: dict, inst: dict) -> None:
    """按 `case.fault` 装桩 —— **改的是 `inst` 里那两个 dict**（同一个对象，见原则 2）。

    两种旋钮：

    - `fault.structured: N` → 结构化输出那条路先失败 N 次（验 `safe_structured` 的兜底）；
    - `fault.stream: N`（可选 `fault.role` 限定哪条链）→ `stream_agent` 前 N 次直接抛。
    """
    # ⑤ 结构化输出的故障注入
    fault_state = inst["fault"]
    n_fault = int((case.get("fault") or {}).get("structured", 0))
    if n_fault:
        fault_state["left"] = n_fault
        orig_get_model = proto.llm.get_model

        def flaky(temperature: float = 0.0):
            return _FlakyStructured(orig_get_model(temperature), fault_state)

        proto.llm.get_model = flaky  # 只影响 safe_structured，主模型调用不受影响

    # ⑤′ **流式抖动注入** —— 让 `stream_agent` 前 N 次调用**直接抛**。
    #
    # 🔴 **它替换的是整个 `stream_agent`（连同内部那次重试）**，所以：
    #      - **一次调用 = 一轮**（`_stream_with_retry` 被绕过了，不会有第二次尝试）；
    #      - `fault.stream: 1` → 第 1 轮（谁先跑就是谁）直接死 → 测**降级**；
    #      - `fault.stream: 2` + `role: executor` → 执行器两次调用都死。
    #    ⚠️ **想测"重试一次救回来"不能用它** —— 那必须让**同一个** `stream_agent` 调用
    #    内部失败一次再成功：单测里用假图做（`unit_stream_join` ④），真跑里把注入点
    #    放在**图的 `stream()`** 上（`experiments/` 里那个跑法）。
    stream_fault_state = inst["stream_fault"]
    _fault_cfg = case.get("fault") or {}
    n_stream_fault = int(_fault_cfg.get("stream", 0))
    if n_stream_fault:
        stream_fault_state["left"] = n_stream_fault
        only_role = str(_fault_cfg.get("role") or "")
        if only_role and only_role not in proto.trace.ROLES:
            raise ValueError(
                f"case.fault.role 只能是 {sorted(proto.trace.ROLES)} 之一，收到 {only_role!r}"
                "（它是**角色枚举**，不是显示名 —— 见 `trace.AGENT` / `trace.EXECUTOR`）"
            )
        orig_stream = proto.stream.stream_agent

        def flaky_stream(*a, **k):  # noqa: ANN002, ANN003, ANN202
            role = k.get("role")
            if role is None and len(a) > 4:  # 位置传参也认
                role = a[4]
            if (stream_fault_state["left"] > 0) and (not only_role or role == only_role):
                stream_fault_state["left"] -= 1
                stream_fault_state["fired"] += 1
                raise TimeoutError("（注入）流式调用抖动")

            return orig_stream(*a, **k)

        proto.stream.stream_agent = flaky_stream
