"""单元级：**评分层的 schema 必须扁平**（不调 LLM）。

    uv run python -m evals.unit_judge

**为什么要有它**（2026-11-09 实测）：`no_repeat_question` 那个 rubric 的 schema
原来写成**嵌套**的（`items: list[_FieldAsked]`）—— Pydantic 于是生成 `$defs` ＋ `$ref`，
而 **DashScope 的 JSON-schema 转换器解析不了**：

    400 … json_schema_converter.cc:4181: Cannot find field $defs in #/$defs/_FieldAsked

后果正是那条 rubric 的 docstring 自己警告过的那种：**调用失败 → 返回 False → 假红**
（"判对了却标红，比漏判更坏"）。⚠️ 它**不是偶发** —— 服务端每次都拒，
所以那个 rubric **从改 schema 那天起一次都没成功过**。

⇒ 判据：**`judge` 里每一个 schema 的字段类型都不许是另一个 `BaseModel`**
（`list[SomeModel]` / `SomeModel` 都算）。要"一个字段一条"就写**平行数组**。

> 有效性自检：把 `_RepeatQuestion` 改回 `items: list[_FieldAsked]`，这里必须红。
"""

# ruff: noqa: T201  # 诊断输出即本产出物

from __future__ import annotations

import inspect
import sys
import typing

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os

# 🔴 **单测的日志打 tag**（2026-11-06）：`logs/` 里真会话与测试垃圾混在一起，复盘时
#    grep 不动 —— 打上 tag 后文件名是 `proto-<ts>-unit-<名字>.log`，一眼可辨。
# ⚠️ **必须排在所有非标准库 import 之前**：`proto.trace` 在 import 那一刻读 env 定文件名。
os.environ["PROTO_LOG_TAG"] = "unit-judge"
os.environ["PROTO_LOG_DIR"] = "tmp/logs"   # 🔴 测试日志是**噪音** —— 别混进给人看的 `logs/`

from pydantic import BaseModel  # noqa: E402

from evals import judge  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail else ""))


def _nested(field_ann: typing.Any) -> list[str]:
    """这个字段的类型里有没有**另一个 BaseModel**（含 `list[...]` 里面那层）。"""
    cands = [field_ann, *typing.get_args(field_ann)]
    return [c.__name__ for c in cands
            if inspect.isclass(c) and issubclass(c, BaseModel) and c is not BaseModel]


def main() -> None:
    print("① 评分层的每个 schema 都是**扁平**的（嵌套会让 DashScope 400 → 假红）")
    # ⚠️ **有效性自检**：把 `_RepeatQuestion` 改回 `items: list[_FieldAsked]` → 必须红。
    bad: list[str] = []
    seen = 0
    for name, obj in vars(judge).items():
        if not (inspect.isclass(obj) and issubclass(obj, BaseModel)) or obj is BaseModel:
            continue
        seen += 1
        for fname, f in obj.model_fields.items():
            hit = _nested(f.annotation)
            if hit:
                bad.append(f"{name}.{fname}: {hit}")
    check(f"judge 里 {seen} 个 schema 都没有嵌套模型", not bad,
          "嵌套的（服务端会 400）：\n        " + "\n        ".join(bad))

    print("\n② 那个改坏的 schema 用的是**平行数组**（保住「没问」的正规写法）")
    rq = getattr(judge, "_RepeatQuestion", None)
    check("`_RepeatQuestion` 有 `fields` ＋ `asked` 两个数组",
          bool(rq) and {"fields", "asked"} <= set(rq.model_fields), repr(rq))

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
