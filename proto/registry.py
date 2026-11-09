"""技能注册表 —— `{name: Skill}` 的**唯一持有者**。

## 这个文件是什么

全进程只有这一张技能表：`skills.py` 在 import 时把内置表登记进来（`register_defaults`），
此后**谁都不许直接拿表** —— 读走 `get()` / `all()` / `values()` / `names()` / `catalog()`，
换表走 `registry.testing.replace()` / `.extend()` / `.reset()`，每次调用查的都是**当前**那一张。
（本模块不认识 `Skill`：只在类型标注里 import 它，见 `if TYPE_CHECKING` 那一行。）

## 设计原则（不变量）

🔴 **对外只给函数，不给 dict。** 任何 `from proto.registry import SKILLS` 的写法
   都是在重新制造一个绑定点 —— 一律走 `get()` / `all()` / `values()`，每次调用查当前表。

🔴 **换表那三个口子在 `testing` 上，不在读 API 的平面上。** 表本身不对外暴露可变引用；
   "这是装配/测试口"由**结构**说（`registry.testing.…`），不靠名字里有没有前缀。〔沿革 1〕

🔴 **未初始化时 `get()` 直接抛错，不静默返回 None。** 静默的后果是"没有这个技能"
   这种看着像业务事实的假话，比当场崩掉难查得多（同 handoff 那条"禁止静默失配"）。

## 关键取舍

**为什么单独一个模块**：这张表原来定义在 `skills.py`，然后被三处各自绑定：

    skills.SKILLS       ← 定义
    agent.py            from proto.skills import SKILLS          （值导入，绑定死在 import 那一刻）
    graph.py            函数内 from proto.skills import SKILLS   （每次调用重新绑定）

由此长出两个病：

1. **反向依赖**：`skills.read_skill` 为了读到"评测替换过的那份表"，得在函数里
   `from proto.agent import SKILLS` —— **领域层依赖编排层**，就为了绕开一个环。
   而那个环本身就是"注册表没有独立的家"造成的。
2. **换一次技能要赋两次值**：`app/runtime.py` 曾经是 `proto.agent.SKILLS = merged`
   之后还得补一句 `proto.skills.SKILLS = merged` —— 漏掉那句，多轮投递时执行器
   "像换了个人"（实测踩过）。

那不是隐患，是**当时靠纪律维持的正确性**。本模块把纪律换成机制。

## 沿革（改过什么、为什么改）

> 编号是**引用锚**（正文里写 `〔沿革 N〕`）—— **只追加、不改号**，新条目加在**末尾**。

1. **换表那三个口子搬进 `testing`**（2026-11-09，第四轮外审 P1-7）。它们原来和
   `get()` / `names()` / `catalog()` 摆在 `_Registry` 的**同一个平面**上 ——
   名字上看不出哪个是测试口，而它们**改的是全进程那一张表**。审计原话：
   「同样存在于生产路径上，**任何一次误调都会改掉全进程的技能表**」。
   ⇒ 新建 `_Testing`，`registry.testing.reset/replace/extend`。
   **"这是测试口"从名字升级成结构** —— 生产代码里写出来会自己看见不对劲。
   ⚠️ 治的是"**测试口混在生产 API 上**"，**不是**"进程级共享"（那是合理的，
   见 `session.py` 沿革 1）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:  # 仅用于类型标注 —— **运行时不 import**，否则 registry ↔ skills 成环
    from proto.skills import Skill


class _Registry:
    """一张内置表 + 一张当前表。

    **为什么要有"内置表"这一层**：`replace` 之后如果直接拿当前表当下次合并的底，
    case 之间就会**串** —— 第一个 case 装过的技能被第二个 case"合并"进来
    （原来 harness 里 `{**proto.skills.SKILLS, **extra}` 就是这个形态）。
    默认表单独存，`reset()` 才有干净的落点。

    ⚠️ **今天走不到**（跑批一 case 一子进程，进程内只 `install()` 一次）—— 留着是因为
    它**本来就该对**：一旦做"同进程多 case"就立刻用得上。
    """

    def __init__(self) -> None:
        #: 内置技能表（`skills.py` 启动时登记）。**只读**，`replace` 不碰它。
        self._defaults: dict[str, Skill] = {}
        #: 当前生效表。**整体替换引用**而不是原地 clear+update —— 并发读的人
        #: 要么看到旧的完整一份、要么看到新的完整一份，不会撞见半张空表。
        self._current: dict[str, Skill] = {}
        self._initialized = False
        #: **测试口** —— 换表那三个动作，见 `_Testing`。
        #: ⚠️ 它和读 API **不在一个平面上**：读是生产的，换表是装配/测试的。
        self.testing = _Testing(self)

    # ──────────────────────────── 写（唯一入口） ────────────────────────────

    def register_defaults(self, skills: Mapping[str, Skill]) -> None:
        """登记内置技能表 —— 由 `skills.py` 在模块级调用一次。同时它就是初始的当前表。"""
        self._defaults = dict(skills)
        self._current = dict(skills)
        self._initialized = True

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

        以前三处各写一遍（`skills.catalog`（已删）、`agent._MAIN_PROMPT` 的拼接、
        harness 的 catalog）—— 改一处忘另一处就是两副面孔。
        """
        self._require_init()
        return "\n".join(f"- {s.name}：{s.description}" for s in self._current.values())

    def _require_init(self) -> None:
        if not self._initialized:
            raise RuntimeError(
                "技能注册表未初始化 —— 没有任何模块 import 过 `proto.skills`。"
                "默认表由它登记；用技能之前请先 `import proto.skills`。"
            )




class _Testing:
    """**换表那三个口子** —— `registry.testing.reset()` / `.replace()` / `.extend()`。

    🔴 **为什么单独一个对象**（2026-11-09）。它们原来和 `get()` / `names()` /
    `catalog()` 摆在 `_Registry` 的**同一个平面**上：名字上看不出哪个是测试口，
    而它们**改的是全进程那一张表**。审计原话 ——「同样存在于生产路径上，
    **任何一次误调都会改掉全进程的技能表**」。
    ⇒ 把"这是测试口"从**名字**升级成**结构**：生产代码里写出 `registry.testing.reset()`
    会**自己看见**不对劲，而不是靠"记得别乱调"。

    ⚠️ **它不解决"进程级共享"** —— 技能表是**静态配置**，进程级共享是合理的
    （`session.py` 沿革 1 那次全仓排查：28 处模块级状态里，真正串数据的**只有产物库**）。
    这里只治"**测试口混在生产 API 上**"。

    ⚠️ **`app/runtime.py` 的 `install()` 会调它，那不是"产品在跑测试"** ——
    `install()` 就是**装配**（跑批每个 case 开始前把技能集重置成内置表），
    而"重置技能表"这个动作本来就**只该在装配那一刻**发生。它和测试调的是同一件事。
    """

    def __init__(self, reg: _Registry) -> None:
        self._reg = reg

    def reset(self) -> None:
        """把当前表恢复成内置表 —— 跑批每个 case 开始前调它。"""
        self._reg._require_init()
        self._reg._current = dict(self._reg._defaults)

    def replace(self, skills: Mapping[str, Skill]) -> None:
        """**整体替换**当前表（评测的 `skills_mode: replace`）。内置表不受影响。"""
        self._reg._require_init()
        self._reg._current = dict(skills)

    def extend(self, extra: Mapping[str, Skill]) -> None:
        """在**当前表**上合并（评测的 `skills_mode: extend`，默认）。

        ⚠️ 合并在**当前表**上而不是内置表上 —— 调用方若要"干净地加几个"，
        先 `reset()` 再 `extend()`。两步是分开的，因为"重置"和"叠加"是两件事。
        """
        self._reg._require_init()
        self._reg._current = {**self._reg._current, **extra}


#: 全进程唯一实例。**只导入这个对象，不要复制表**。
registry = _Registry()
