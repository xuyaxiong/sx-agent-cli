"""单元级：**技能注册表的唯一入口**（R1，不调 LLM）。

    uv run python -m evals.unit_registry

## 为什么单独一个单测

R1 修掉的是"同一张表有三个绑定点"：

    skills.SKILLS（定义） / agent 的值导入 / graph 的函数内导入

外加一条**领域层反向依赖编排层**的 lazy import（`skills.read_skill` → `proto.agent.SKILLS`），
就是为了绕开"注册表没有独立的家"造成的环。

这类问题**不会自己报错**：改一处，另一处静默用旧表。实测后果是"用户答了，
执行器却像换了个人"（多轮投递按内置技能重跑）。所以：

🔴 **每条断言都写成"谁把老写法搬回来，这里必须红"。**

## 管这些

**① 依赖方向**（AST 静态扫，不看注释）
   - `registry.py` **顶层**不许有 proto 内依赖（否则 registry ↔ skills 成环）
   - `skills.py` 不许 import `proto.agent` / `proto.executor` / `proto.graph`
     —— 包括**函数内**（当初那条反向 lazy import 就藏在函数里）
   - 全仓不许出现 `from proto.skills import SKILLS`（那是又造一个绑定点）
   - 全仓不许往 `*.SKILLS` 上**写**（"赋两次值"的回潮）—— 判据是**形状**不是拼写：
     `x.SKILLS = m` / `x.SKILLS: dict = m` / `x.SKILLS |= m` / `setattr(x, "SKILLS", m)`
     四种一起管（原来只看第一种，后三种全漏 —— 2026-11-03 审核实测）
   - **proto 运行时 import 图无环**（含函数内；`if TYPE_CHECKING:` 不算）——
     `skills` ↔ `skill_pack` 那个环原来靠函数内 lazy import 掩盖（2026-11-05 把两个
     读工具搬去 `skill_pack` 后消失）；这条盯着**任何**新环

**② 运行时语义**
   - `replace` 之后 **`read_skill` 必须看到新表** —— 这条是 R1 的核心验收：
     原来它读 `proto.agent.SKILLS`，只改 `proto.skills.SKILLS` 就分叉
   - `replace` 不污染内置表（`reset()` 能回去）；`extend` 叠在当前表上
   - `all()` 给副本（改不到注册表）；`get()` 查不到返回 None（业务事实）
   - **未初始化直接抛错**，不静默返回空表

**③ 单一排版来源**
   - 技能清单 `- 名字：描述` 只在 `registry.catalog()` 里拼一次
     （判据同样是**形状**：同一行既插 `.name` 又插 `.description` 就是在拼它，
     换个变量名也认得出来）

**④ 装配路径**（`harness.install`）—— 注册表 API 之外的**那一段**：
   - `replace` 装上的技能，下一个**不带技能**的 case 必须回到内置表
     （`install()` 里"先 reset 再叠加"是这次唯一改运行时行为的地方）
"""

from __future__ import annotations

import ast
import asyncio
import io
import re
from pathlib import Path

import os

# 🔴 **单测的日志打 tag**（2026-11-06）：`logs/` 里真会话与测试垃圾混在一起，复盘时
#    grep 不动 —— 打上 tag 后文件名是 `proto-<ts>-unit-<名字>.log`，一眼可辨
#    （清理：`rm logs/*-unit-*.log`）。复用跑批那个 `PROTO_LOG_TAG`（`evals/_child.py`）。
# ⚠️ **必须排在所有非标准库 import 之前**：`proto.trace` 在 import 那一刻读 env 定文件名，
#    而 `from app import chat` 这种**间接** import 也会把它拉进来（实测踩过：
#    第一版插在 `from app import chat` 之后 → 日志照样没 tag）。
#    守卫：`unit_feed_waiting` ⑮。
os.environ["PROTO_LOG_TAG"] = "unit-registry"
os.environ["PROTO_LOG_DIR"] = "tmp/logs"   # 🔴 测试日志是**噪音** —— 别混进给人看的 `logs/`

from proto import skill_pack
from proto.registry import _Registry, registry

ROOT = Path(__file__).resolve().parent.parent

#: 技能清单的**形状**：同一行里插了 `.name`、**一个冒号**、又插了 `.description`
#: —— 那就是在拼清单那一行（`- 名字：描述`）。变量名叫什么不管。
#:
#: ⚠️ 分隔符收在**冒号**上，不再放宽：`【技能 {x.name}】{x.description}` 那种是
#: **另一种渲染**（`read_skill` 的抬头），不是清单 —— 放宽了就是假阳性（实测踩过）。
#: ⚠️ 正则拆成 raw 串再拼 —— 否则本文件自己就被这条检查扫中（同下面 ③ 的注释）。
_NAME_FIELD = r"\{[^}\n]*\.name[^}\n]*\}"
_DESC_FIELD = r"\{[^}\n]*\.description"
_CATALOG_SHAPE = re.compile(_NAME_FIELD + r"[^\n]*[：:][^\n]*" + _DESC_FIELD)

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail and not ok else ""))


def _py_files(*subdirs: str) -> list[Path]:
    out: list[Path] = []
    for d in subdirs:
        out += [p for p in (ROOT / d).rglob("*.py") if "__pycache__" not in p.parts]
    return sorted(out)


def _tree(p: Path) -> ast.Module:
    return ast.parse(io.open(p, encoding="utf-8").read(), str(p))


def _skill_writes(tree: ast.Module) -> list[int]:
    """所有"往 `*.SKILLS` 上写"的行号 —— 判**形状**，不判某一种拼写。

    🔴 2026-11-03 审核实测：原来只看 `ast.Assign`，于是
    `x.SKILLS: dict = m`（AnnAssign）、`x.SKILLS |= m`（AugAssign）、
    `setattr(x, "SKILLS", m)` 三种老写法**全漏**。
    """
    out: list[int] = []
    for n in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(n, ast.Assign):
            targets = list(n.targets)
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)):
            targets = [n.target]
        if any(isinstance(t, ast.Attribute) and t.attr == "SKILLS" for t in targets):
            out.append(n.lineno)
        # `setattr(x, "SKILLS", m)` —— 同一个动作的另一种写法
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "setattr"
            and len(n.args) >= 2
            and isinstance(n.args[1], ast.Constant)
            and n.args[1].value == "SKILLS"
        ):
            out.append(n.lineno)
    return out


def _imports(p: Path, *, top_only: bool) -> list[tuple[int, str]]:
    """`(行号, 模块名)`。`top_only=True` 时**不钻 if/try 块**

    —— `if TYPE_CHECKING:` 里的 import 运行时不执行，正是靠这一条放过它。
    """
    nodes = _tree(p).body if top_only else list(ast.walk(_tree(p)))
    out: list[tuple[int, str]] = []
    for n in nodes:
        if isinstance(n, ast.Import):
            out += [(n.lineno, a.name) for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            out.append((n.lineno, n.module or ""))
    return out


def main() -> None:
    files = _py_files("proto", "evals")
    print(f"扫描 {len(files)} 个 .py（proto/ + evals/）\n")

    # ─────────────────────────── ① 依赖方向 ───────────────────────────

    print("① 依赖方向（AST，不看注释）")

    reg_top = _imports(ROOT / "proto/registry.py", top_only=True)
    bad = [f"{m}:{ln}" for ln, m in reg_top if m.startswith("proto")]
    check("registry.py **顶层**零 proto 依赖（TYPE_CHECKING 里的不算）", not bad, repr(bad))
    guarded = _imports(ROOT / "proto/registry.py", top_only=False)
    check(
        "registry 对 skills 的引用只在类型标注块里",
        any(m == "proto.skills" for _, m in guarded) and not bad,
        repr(guarded),
    )

    ORDER = ("proto.agent", "proto.executor", "proto.graph")
    rev = [
        f"{p.relative_to(ROOT)}:{ln}→{m}"
        for p in _py_files("proto")
        if p.name == "skills.py"
        for ln, m in _imports(p, top_only=False)  # 函数内也要查：当初就藏在函数里
        if m in ORDER
    ]
    check("skills.py 不反向依赖编排层（含函数内）", not rev, repr(rev))

    stale = [
        f"{p.relative_to(ROOT)}:{ln}"
        for p in files
        for ln, mod, name in [
            (n.lineno, n.module, a.name)
            for n in ast.walk(_tree(p))
            if isinstance(n, ast.ImportFrom) and n.module
            for a in n.names
        ]
        if mod == "proto.skills" and name == "SKILLS"
    ]
    check("全仓没有 `from proto.skills import SKILLS`（没有第二个绑定点）", not stale, repr(stale))

    assigned = [f"{p.relative_to(ROOT)}:{ln}" for p in files for ln in _skill_writes(_tree(p))]
    check("全仓没有往 `*.SKILLS` 上写（\"赋两次值\"不许回潮）", not assigned, repr(assigned))

    # ── proto 运行时 import 图**无环**（2026-11-05，F3 的机制）──
    # 🔴 为什么是机制：`skills` ↔ `skill_pack` 那个环原来靠**函数内 lazy import** 掩盖 ——
    #    实测：把它提到顶层**当场 ImportError**
    #    （"partially initialized module"）。两个读工具搬去 `skill_pack` 后环消了；
    #    这条盯着**任何**新环 —— **包括藏进函数里的**（藏起来正是它原来的形态）。
    # ⚠️ 口径 = **运行时真会执行的 import**：函数内的**算**；`if TYPE_CHECKING:` 里的
    #    **不算**（运行时不执行 —— registry 那条是正规写法，不是环）。
    def _runtime_imports(p: Path) -> list[tuple[int, str, list[str]]]:
        """(行号, 模块, 名字们)。模块级**不钻** if/try 块；函数体的直接子句算。"""
        tree = _tree(p)
        out: list[tuple[int, str, list[str]]] = []

        def grab(nodes: list[ast.stmt]) -> None:
            for n in nodes:
                if isinstance(n, ast.Import):
                    out.extend((n.lineno, a.name, []) for a in n.names)
                elif isinstance(n, ast.ImportFrom):
                    out.append((n.lineno, n.module or "", [a.name for a in n.names]))

        grab(tree.body)
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                grab(n.body)
        return out

    proto_files = _py_files("proto")
    mod_of = {
        p: ("proto." + ".".join(p.relative_to(ROOT / "proto").with_suffix("").parts))
        .removesuffix(".__init__")
        for p in proto_files
    }
    g: dict[str, set[str]] = {m: set() for m in mod_of.values()}
    for p in proto_files:
        for _ln, mod, names in _runtime_imports(p):
            for t in (mod, *(f"{mod}.{nm}" for nm in names)):
                if not t.startswith("proto"):
                    continue
                parts = t.split(".")
                # 连带父包：import 子模块会先执行父包的 __init__
                for i in range(2, len(parts) + 1):
                    anc = ".".join(parts[:i])
                    if anc in g:
                        g[mod_of[p]].add(anc)

    GRAY, BLACK = 1, 2
    color: dict[str, int] = {}
    stack: list[str] = []
    cycles: list[str] = []

    def dfs(m: str) -> None:
        color[m] = GRAY
        stack.append(m)
        for nxt in sorted(g[m]):
            if color.get(nxt) == GRAY:
                cycles.append(" → ".join([*stack[stack.index(nxt):], nxt]))
            elif nxt not in color:
                dfs(nxt)
        stack.pop()
        color[m] = BLACK

    for m in sorted(g):
        if m not in color:
            dfs(m)
    check("proto 运行时 import 图**无环**（含函数内；TYPE_CHECKING 不算）",
          not cycles, repr(cycles))

    # ── 产品包**不许** import 测试包（2026-11-09 搬 `app/` 时立的机制）──
    # 🔴 为什么是机制：`chat.py` 原来 `from evals.harness import ...`（8 样）
    #    ＋ `from evals.run import _append_trace` —— **产品依赖测试包**，于是
    #    "分发单元"和"测试单元"混居、`evals/` 没法整体排除出生产镜像。
    #    搬完得有东西盯着它**别长回来**：一次 `from evals.xxx import` 就复发，
    #    而且**一个字都不报错**（跑得好好的，只是包里多带了一份测试代码）。
    #    ⚠️ **反方向不管** —— 测试 import 产品是对的（`unit_*` 全靠它）。
    #    有效性自检：在 `app/chat.py` 里写一行 `from evals import checks` → 这里必须红。
    _leaks: list[str] = []
    for _pkg in ("proto", "app"):
        for _p in _py_files(_pkg):
            for _ln, _mod, _names in _runtime_imports(_p):
                if _mod == "evals" or _mod.startswith("evals."):
                    _leaks.append(f"{_p.relative_to(ROOT)}:{_ln} → {_mod}")
    check("`proto/` / `app/` **不 import** `evals/`（产品不许依赖测试包）",
          not _leaks, repr(_leaks))

    # ─────────────────────────── ② 运行时语义 ───────────────────────────

    print("\n② 运行时语义（换表之后所有人都得看见）")

    from proto.skills import Skill

    builtin = registry.names()
    registry.replace({"only-one": Skill(name="only-one", description="临时", instructions="干活")})
    out = asyncio.run(skill_pack.read_skill.ainvoke({"skill_name": "only-one"}))
    miss = asyncio.run(skill_pack.read_skill.ainvoke({"skill_name": "lesson-prep"}))
    check("replace 后 `read_skill` 看到新表（核心验收点）", "only-one" in out, repr(out[:60]))
    check("replace 后旧技能不在表里（不是「两份表并存」）", "没有技能" in miss, repr(miss[:60]))

    registry.reset()
    check("`replace` 不污染内置表，`reset()` 能回来", registry.names() == builtin,
          f"{registry.names()} != {builtin}")

    registry.extend({"extra": Skill(name="extra", description="d", instructions="i")})
    check("`extend` 叠在当前表（内置 + 新增）",
          "extra" in registry.names() and "lesson-prep" in registry.names(), repr(registry.names()))
    registry.reset()

    d = registry.all()
    d["hack"] = 1
    check("`all()` 返回副本（改不到注册表）", "hack" not in registry.names(), repr(registry.names()))

    check("`get()` 查不到返回 None（业务事实，不是异常）", registry.get("不存在的技能") is None)

    fresh = _Registry()
    try:
        fresh.get("x")
        raised = False
    except RuntimeError:
        raised = True
    check("未初始化时**抛错**，不静默返回空表", raised)

    # ─────────────────────────── ③ 单一排版来源 ───────────────────────────

    print("\n③ 单一排版来源")

    # ⚠️ 正则**拼出来**而不是写字面量 —— 否则**本文件自己**就成了"第 N 处手抄"，
    #    检查器扫到自己（第一次跑就是这样红的）。这比"排除自己"更干净：
    #    排除等于给检查开后门，拼接只是让检查器不产生假阳性。
    # 🔴 判**形状**（名字字段 + 冒号 + 描述字段），不判变量名叫什么 ——
    #    原来写死一串字面拼写，换个变量名就漏（2026-11-03 审核实测）。
    dup = [
        str(p.relative_to(ROOT))
        for p in files
        if p.name != "registry.py" and _CATALOG_SHAPE.search(io.open(p, encoding="utf-8").read())
    ]
    check("技能清单只在 `registry.catalog()` 里拼（不是三处手抄）", not dup, repr(dup))

    # ─────────────────────────── ④ 装配路径 ───────────────────────────

    print("\n④ 装配路径：`install()` 先 reset 再叠加（注册表 API 之外的那一段）")
    # 🔴 `install()` 里那句 `registry.reset()` 是这次**唯一改运行时行为**的地方
    # （空 case 不再继承上一个 case 的表）—— 而 ② 只覆盖注册表 API，覆盖不到它。
    from app.runtime import install

    builtin = registry.names()
    install({"skills": [{"name": "只此一个", "description": "d", "instructions": "i"}],
             "skills_mode": "replace"})
    check("replace 模式装上了（且内置的不在了）", registry.names() == ["只此一个"],
          repr(registry.names()))
    install({})  # 下一个 case：不带任何技能
    check("**空 case 的 install 回到内置表**（不继承上一个 case 的替换）",
          registry.names() == builtin, f"{registry.names()} != {builtin}")

    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
