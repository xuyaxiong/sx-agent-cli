"""技能注册表 —— `{name: Skill}` 的**唯一持有者**。

## 为什么单独一个模块

原来这张表定义在 `skills.py`，然后被三处各自绑定：

    skills.SKILLS       ← 定义
    agent.py:45         from proto.skills import SKILLS          （值导入，绑定死在 import 那一刻）
    graph.py:107        函数内 from proto.skills import SKILLS   （每次调用重新绑定）

由此长出两个病：

1. **反向依赖**：`skills.read_skill` 为了读到"评测替换过的那份表"，得在函数里
   `from proto.agent import SKILLS` —— **领域层依赖编排层**，就为了绕开一个环。
   而那个环本身就是"注册表没有独立的家"造成的。
2. **换一次技能要赋两次值**：`evals/harness.py` 曾经是这样

       proto.agent.SKILLS = merged
       proto.skills.SKILLS = merged     # 漏这句 → 多轮投递时执行器"像换了个人"（实测踩过）

   那不是隐患，是**当时靠纪律维持的正确性**。本模块把纪律换成机制。

## 铁律

🔴 **对外只给函数，不给 dict。** 任何 `from proto.registry import SKILLS` 的写法
   都是在重新制造一个绑定点 —— 一律走 `get()` / `all()` / `values()`，每次调用查当前表。

🔴 **唯一变更入口是 `replace` / `extend` / `reset`。** 表本身不对外暴露可变引用。

🔴 **未初始化时 `get()` 直接抛错，不静默返回 None。** 静默的后果是"没有这个技能"
   这种看着像业务事实的假话，比当场崩掉难查得多（同 handoff 那条"禁止静默失配"）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:  # 仅用于类型标注 —— **运行时不 import**，否则 registry ↔ skills 成环
    from proto.skills import Skill


class _Registry:
    """一张内置表 + 一张当前表。

    **为什么要有"内置表"这一层**（不是为了好看）：`replace` 之后如果直接拿当前表当
    下次合并的底，case 之间就会**串**。原来 harness 里 `{**proto.skills.SKILLS, **extra}`
    就是这个形态 —— 第一个 replace 的 case 跑完，`proto.skills.SKILLS` 已经是它那份了，
    第二个 case 再"合并"就把前一个 case 的技能带进来。

    ⚠️ **今天走不到**（跑批一 case 一子进程，进程内只 `install()` 一次）—— 留着是因为
    它**本来就该对**：一旦做"同进程多 case"就立刻用得上。默认表单独存，`reset()` 才干净。
    """

    def __init__(self) -> None:
        #: 内置技能表（`skills.py` 启动时登记）。**只读**，`replace` 不碰它。
        self._defaults: dict[str, Skill] = {}
        #: 当前生效表。**整体替换引用**而不是原地 clear+update —— 并发读的人
        #: 要么看到旧的完整一份、要么看到新的完整一份，不会撞见半张空表。
        self._current: dict[str, Skill] = {}
        self._initialized = False

    # ──────────────────────────── 写（唯一入口） ────────────────────────────

    def register_defaults(self, skills: Mapping[str, Skill]) -> None:
        """登记内置技能表 —— 由 `skills.py` 在模块级调用一次。同时它就是初始的当前表。"""
        self._defaults = dict(skills)
        self._current = dict(skills)
        self._initialized = True

    def reset(self) -> None:
        """把当前表恢复成内置表 —— 跑批每个 case 开始前调它。"""
        self._require_init()
        self._current = dict(self._defaults)

    def replace(self, skills: Mapping[str, Skill]) -> None:
        """**整体替换**当前表（评测的 `skills_mode: replace`）。内置表不受影响。"""
        self._require_init()
        self._current = dict(skills)

    def extend(self, extra: Mapping[str, Skill]) -> None:
        """在**当前表**上合并（评测的 `skills_mode: extend`，默认）。

        ⚠️ 合并在**当前表**上而不是内置表上 —— 调用方若要"干净地加几个"，
        先 `reset()` 再 `extend()`。两步是分开的，因为"重置"和"叠加"是两件事。
        """
        self._require_init()
        self._current = {**self._current, **extra}

    # ──────────────────────────── 读 ────────────────────────────

    def get(self, name: str) -> Skill | None:
        """按名取技能；没有就返回 None（"表里没有"是业务事实，由调用方决定怎么回话）。"""
        self._require_init()
        return self._current.get(name)

    def all(self) -> dict[str, Skill]:
        """当前表**副本** —— 调用方随便改，改不到注册表。"""
        self._require_init()
        return dict(self._current)

    def values(self) -> list[Skill]:
        self._require_init()
        return list(self._current.values())

    def names(self) -> list[str]:
        self._require_init()
        return list(self._current)

    def catalog(self) -> str:
        """技能清单的**唯一排版来源**（渐进式披露第一层）。

        原来是三处各写一遍（`skills.catalog`（已删）、`agent._MAIN_PROMPT` 的拼接、
        harness 的 catalog），改一处忘另一处就两副面孔。
        """
        self._require_init()
        return "\n".join(f"- {s.name}：{s.description}" for s in self._current.values())

    def _require_init(self) -> None:
        if not self._initialized:
            raise RuntimeError(
                "技能注册表未初始化 —— 没有任何模块 import 过 `proto.skills`。"
                "默认表由它登记；用技能之前请先 `import proto.skills`。"
            )


#: 全进程唯一实例。**只导入这个对象，不要复制表**。
registry = _Registry()
