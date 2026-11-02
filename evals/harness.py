"""跑 agent 的**装配 + 记录层** —— 交互式 CLI 与跑批**共用**的那部分。

    from evals.harness import install, snapshot, dump_run_record

**装什么**：技能（从目录读，或 case 内联）→ 换进 `proto.agent.SKILLS`；
再把 `store_artifact` / `run_task` / 模型调用包一层**记录器**
（产物流水、执行器流水、token 计数）。

**录什么**：`snapshot()` 抓一轮的全量快照；`dump_artifacts()` / `dump_run_record()`
把它落成人能读的文件（逐轮全记录 + 软判断留档 + 原始 trace 附录）。

🔴 **为什么两个入口必须共用这一层**：它们要产**同一种记录格式**。
交互式入口就是"把脚本预置的 turns 换成真人当场打的字"—— 格式不一致的话，
收回来的一堆 run 没法比，而"比"正是现在要做的事（见 `chat.py` 的 docstring）。

⚠️ **来历**（2026-10-30）：从 `_child.py` 拆出来。原来 803 行里只有 68 行
（`run_case` + `main`）是跑批独有的，其余全是共用的 —— 但文件名和 docstring
只描述了那 68 行，于是 `chat.py` 的 `from evals._child import ...`
看着像"产品依赖测试代码"。**共享是故意的，错的是名字。**
"""

from __future__ import annotations

import shutil
import sys
import threading
import time
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage

from proto import config as proto_config
from proto.skill_pack import ENTRY_NAME
from proto.config import app_root


# ──────────────────────────── token / 调用计数 ────────────────────────────


class UsageCounter(BaseCallbackHandler):
    """挂在模型实例上，数**所有** LLM 调用（主 agent + 执行器 + 判定）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.in_tokens = 0
        self.out_tokens = 0
        self.per_call: list[dict] = []

    def on_llm_end(self, response, **kwargs) -> None:  # noqa: ANN001
        self.calls += 1
        u = None
        try:
            u = response.generations[0][0].message.usage_metadata
        except Exception:  # noqa: BLE001
            pass
        if u:
            self.in_tokens += u.get("input_tokens", 0)
            self.out_tokens += u.get("output_tokens", 0)
        self.per_call.append(
            {"in": (u or {}).get("input_tokens", 0), "out": (u or {}).get("output_tokens", 0)}
        )

    def snapshot(self) -> dict:
        return {
            "llm_calls": self.calls,
            "in_tokens": self.in_tokens,
            "out_tokens": self.out_tokens,
            "total_tokens": self.in_tokens + self.out_tokens,
        }


# ──────────────────────────── 故障注入 ────────────────────────────


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


# ──────────────────────────── 应用根 / 目录约定 ────────────────────────────

#: PyInstaller 打的包里 `sys.frozen` 为真 —— 目录约定据此分叉。
FROZEN = bool(getattr(sys, "frozen", False))
#: 应用根：冻结后 = exe 所在目录，开发时 = `langgraph-lab/`（见 `proto.config.app_root`）。
ROOT = app_root()
#: 只读资源（随包带）：冻结后是 `sys._MEIPASS`，开发时同 ROOT。
RESOURCE_ROOT = proto_config.RESOURCE_ROOT

#: **技能目录的默认值** —— 冻结后是 exe 旁边的 `skills/`（用户往里丢 `.md` 就加技能）；
#: 开发时还是仓库里的 `evals/skills/`。
DEFAULT_SKILLS_DIR = (ROOT / "skills") if FROZEN else (ROOT / "evals" / "skills")

#: **运行记录落哪** —— 冻结后是 exe 旁边的 `runs/`（便携、用户找得到、方便整包发回来）；
#: 开发时还是仓库里的 `evals/runs/`（文档和历史记录都指这里）。
RUNS_DIR = (ROOT / "runs") if FROZEN else (ROOT / "evals" / "runs")

#: 首次运行时铺进用户技能目录的**示例技能**（让他们知道 `.md` 该长什么样）。
#: 从只读资源里取 —— 开发时就是 `evals/skills/` 本身。
SEED_SKILLS_DIR = RESOURCE_ROOT / "evals" / "skills"


def ensure_app_dirs() -> list[str]:
    """首次运行把目录铺好 —— **只在冻结后做**（开发时目录本来就在）。

    建 `skills/` + `runs/`；`skills/` 是空的就往里放一遍示例技能。
    返回做了哪些事（给启动时打一行用）。
    """
    if not FROZEN:
        return []
    done: list[str] = []
    for d in (DEFAULT_SKILLS_DIR, RUNS_DIR):
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            done.append(f"建目录 {d.name}/")
    if not any(DEFAULT_SKILLS_DIR.glob(f"*/{ENTRY_NAME}")) and SEED_SKILLS_DIR.is_dir():
        n = 0
        for src in sorted(p for p in SEED_SKILLS_DIR.iterdir() if p.is_dir()):
            # 只铺**真能当技能**的目录：有 `SKILL.md` 才算
            # （源目录里的 `README.md` 是适配说明，不是技能 —— 铺过去只会让人困惑）
            if not (src / ENTRY_NAME).is_file():
                continue
            shutil.copytree(src, DEFAULT_SKILLS_DIR / src.name, dirs_exist_ok=True)
            n += 1
        if n:
            done.append(f"放入 {n} 个示例技能")
    return done


# ──────────────────────────── 技能加载 ────────────────────────────


def _load_skills_dir(path_str: str) -> dict:
    """从目录装技能包（相对路径按**仓库根**解析，子进程 cwd 变了也不受影响）。

    🔴 **格式解析不在这里** —— "技能包长什么样、怎么读"是**领域知识**，
    在 `proto.skill_pack`（`Skill` / `Reference` 两个领域对象在 `proto.skills`）。
    这里只做一件事：**把相对路径定到仓库根，然后交给它**。
    """
    # ⚠️ **局部导入是有意的**：`proto.trace` 一被导入就开日志文件（模块级的 `_fh`），
    # 而 `harness` 也被 `report.py` 这类**不该产生日志**的地方导入。**别提到模块级去。**
    from proto import trace
    from proto.skill_pack import load_skills_dir

    root = Path(path_str)
    if not root.is_absolute():
        root = ROOT / root
    skills, warn = load_skills_dir(root)
    for w in warn:
        trace.note(f"技能加载：{w}")
    return skills


# ──────────────────────────── 装配 ────────────────────────────


def install(case: dict) -> dict:
    """装好全部观测与注入。**必须在 `build()` 之前调用**。"""
    import proto.agent
    import proto.config
    import proto.executor
    import proto.llm
    import proto.artifacts
    import proto.skills

    # ① 产物库清空 —— 全局 dict，不清就会带上上一轮
    proto.artifacts.ARTIFACTS.clear()

    # ② 技能集（case 可以带自己的：`skills_dir` 读目录 / `skills` 内联）
    extra: dict = {}
    if case.get("skills_dir"):
        extra.update(_load_skills_dir(str(case["skills_dir"])))
    if case.get("skills"):
        extra.update({s["name"]: proto.skills.Skill(**s) for s in case["skills"]})
    if extra:
        merged = (
            extra
            if case.get("skills_mode", "extend") == "replace"
            else {**proto.skills.SKILLS, **extra}
        )
        proto.agent.SKILLS = merged
        # 🔴 `graph._feed_waiting` 里是**函数内** `from proto.skills import SKILLS` ——
        #    只换 `proto.agent.SKILLS` 的话，多轮投递（用户答复 → 待答任务）会按
        #    **内置技能**重跑，或找不到而跳过：表现为"用户答了，执行器却像换了个人"。
        #    实测踩点：`skills_mode: replace` + 多轮 + 有等待任务的 case 才会触发。
        proto.skills.SKILLS = merged

    # ③ 产物写入流水 —— 记「谁在什么时候写了什么」，用来判并发与归属
    # ⚠️ **签名要跟着 `proto.artifacts.store_artifact` 走**：它变过（加 `file_type`），
    # 漏改这里就是整轮 `TypeError` —— 和 `run_task` 那个包装同一个坑。
    writes: list[dict] = []
    orig_store = proto.artifacts.store_artifact

    def rec_store(title: str, summary: str, file_type: str, content: str) -> str:
        aid = orig_store(title, summary, file_type, content)
        writes.append(
            {
                "aid": aid,
                "title": title,
                "summary": summary,
                "type": file_type,
                "chars": len(content),
                "thread": threading.get_ident(),
                "t": round(time.time(), 3),
            }
        )
        return aid

    # `write_artifact` 的工具体在 `skills.py` 里按**模块全局**找 `store_artifact`，所以这里改得到
    proto.artifacts.store_artifact = rec_store

    # ③′ 执行器调用流水 —— 记「哪个线程在跑哪个 skill」，用来判产物归属
    #     （`agent.py` 是 `from proto.executor import run_task` 值导入，两处都得换）
    run_tasks: list[dict] = []
    orig_run_task = proto.executor.run_task

    def rec_run_task(skill, briefing: str = "", user_turns=None):
        # 本次 run **前后**产物库的差集 = 这次真写出来的。
        # ⚠️ 不能按线程归属：产物写入发生在 langgraph 的工具线程里，和 run_task 的线程
        #    对不上（实测 threads 完全不重合），按线程判会**永远不报**。
        # ⚠️ 签名要跟着 `proto.executor.run_task` 走：它变过两次（`given`→`briefing`、
        #    去掉 `artifacts` 参数），**每次都漏改这里、每次都整轮崩** —— 改签名时记得搜一遍。
        before = set(proto.artifacts.ARTIFACTS)
        out = orig_run_task(skill, briefing, user_turns)
        run_tasks.append(
            {
                "thread": threading.get_ident(),
                "skill": skill.name,
                "t": round(time.time(), 3),
                # 这次真交付的产物（差集）
                "produced": sorted(set(proto.artifacts.ARTIFACTS) - before),
            }
        )
        return out

    proto.executor.run_task = rec_run_task
    proto.agent.run_task = rec_run_task

    # ④ 执行路径开关（case 可选）—— 默认 `"auto"`（两条路都开，产品行为）。
    # 🔴 **它是"平台造路"**：想验"执行器的正文交付"就得先让委派**真的发生**，
    # 而"走哪条路"是模型的自由选择 —— 靠碰运气压路径的断言必然脆。
    # 见 `proto/agent.EXEC_PATH` 的 docstring。
    proto.agent.EXEC_PATH = str(case.get("exec_path") or "auto")

    # ④ token / 调用计数 —— 三处值导入一起换
    counter = UsageCounter()
    orig_get_model = proto.config.get_model

    def counting(temperature: float = 0.0):
        return orig_get_model(temperature).model_copy(update={"callbacks": [counter]})

    proto.agent.get_model = counting
    proto.executor.get_model = counting
    proto.llm.get_model = counting

    # ⑤ 故障注入（可选）
    fault_state = {"left": 0, "fired": 0}
    n_fault = int((case.get("fault") or {}).get("structured", 0))
    if n_fault:
        fault_state["left"] = n_fault

        def flaky(temperature: float = 0.0):
            return _FlakyStructured(counting(temperature), fault_state)

        proto.llm.get_model = flaky  # 只影响 safe_structured，主模型调用不受影响

    return {
        "counter": counter,
        "writes": writes,
        "run_tasks": run_tasks,
        "fault": fault_state,
        "skills_mod": proto.skills,
    }


# ──────────────────────────── 快照 ────────────────────────────


#: 主 agent 上下文**告警线** —— **只告警，不判红**（2026-10-30 用户定：
#: *"删掉这个测试，把这个信息记录到日志 warning 里面不就完了"*）。
#:
#: 为什么不做成断言：**读产物是主 agent 的合法能力**（它也要能干活、能按需读），
#: 拿上下文大小判红 = 罚一个合法能力，而且聊得久、多读一次技能原文都会误伤。
#: 告警线给的是**读数 + 一句提示**，代价由看的人自己判断。
#:
#: 参照读数（历史跑批）：单技能隔离场景 517–1109 字；多技能长产物场景 7890–14832 字。
CTX_WARN_CHARS = 12000


def ctx_warn(total: int) -> str:
    """超告警线时给一句提示，正常时返回空串。**只提示，不判红。**"""
    if total <= CTX_WARN_CHARS:
        return ""
    return (
        f"⚠️ 主上下文超告警线 {CTX_WARN_CHARS} 字"
        "（看看是不是把产物正文读进来了 —— 依赖转交只需传 ID，不必读全文）"
    )


def _ctx_chars(messages: list) -> int:
    """主 agent 上下文体积 —— 与 `proto/__main__._ctx_chars` **同一口径**（可比）。"""
    total = 0
    for m in messages:
        total += len(str(m.content))
        for c in getattr(m, "tool_calls", None) or []:
            total += len(str(c.get("args", "")))
    return total


def _last_ai_text(state: dict) -> str:
    for m in reversed(state.get("messages") or []):
        if isinstance(m, AIMessage) and str(m.content).strip():
            return str(m.content)
    return ""


def _sys_prompt_chars() -> int:
    """主 agent 的 system prompt 体积。

    ⚠️ **它不在 `state["messages"]` 里**（`create_agent(system_prompt=...)` 不落 state），
    所以 `main_ctx_chars` 量不到它。口径与 `proto/__main__._ctx_chars` 保持一致（可比），
    但真实上下文 = `main_ctx_chars + sys_prompt_chars`，这个数也记下来。
    """
    import proto.agent
    from proto.skills import SKILLS

    catalog = "\n".join(f"- {s.name}：{s.description}" for s in SKILLS.values())
    return len(proto.agent._MAIN_PROMPT) + len("\n\n可用能力：\n" + catalog)


def snapshot(state: dict, user: str, prev_len: int, idx: int, wall_s: float) -> dict:
    from proto import trace
    from proto.artifacts import ARTIFACTS

    msgs = state.get("messages") or []
    new = msgs[prev_len:]
    delegates = [
        c.get("name")
        for m in new
        for c in (getattr(m, "tool_calls", None) or [])
        if c.get("name") == "delegate"
    ]
    #: **全部**工具调用（名字 + 参数摘要）—— 主 agent 读了哪个产物、写了什么，
    #: 这里直接可查。原来只记 delegate，`read_artifact` 只能靠"消息类型对不上"反推。
    tool_calls = [{"name": n, "args": a} for n, a in trace.tool_calls_of(new)]
    return {
        "turn": idx,
        "user": user,
        #: 这句话是谁给的 —— `scripted`（case YAML 预置）/ `human`（`evals.chat` 当场打的）。
        #: 日志必须如实标：读的人不能把"我编的答复"当成真实用户行为。
        "input_source": "scripted",
        "reply": _last_ai_text(state),
        "tasks": state.get("tasks") or [],
        "main_ctx_chars": _ctx_chars(msgs),
        "sys_prompt_chars": _sys_prompt_chars(),
        "artifacts_chars": sum(len(v["content"]) for v in ARTIFACTS.values()),
        "artifacts": {k: len(v["content"]) for k, v in ARTIFACTS.items()},
        "delegate_calls": delegates,
        "tool_calls": tool_calls,
        #: 本轮**全部新消息**（原文，不截断）—— 逐轮审计文件靠它渲染"运行全记录"：
        #: 用户输入 / 主 agent 输出 / 工具调用与返回 / 平台注入的台账，一条都不落。
        #: `tool_call_id` 与 `tool_calls[].id` 配对，用来判**这条返回是谁的**。
        "messages": [
            {
                "kind": type(m).__name__,
                "content": str(m.content),
                "tool_call_id": getattr(m, "tool_call_id", None),
                "tool_calls": [
                    {"id": c.get("id"), "name": c.get("name"), "args": c.get("args")}
                    for c in (getattr(m, "tool_calls", None) or [])
                ],
            }
            for m in new
        ],
        "new_msg_kinds": [type(m).__name__ for m in new],
        "log": state.get("log") or [],
        "wall_s": wall_s,
    }


# ──────────────────────────── 落盘：给人工审计用 ────────────────────────────


def _slug(s: str, n: int = 40) -> str:
    """转发 `proto.artifacts.artifact_slug` —— 文件名怎么起，落盘和这里必须是同一套。"""
    from proto.artifacts import artifact_slug

    return artifact_slug(s, n)


def dump_artifacts(out_dir: Path, case_id: str, rep: int, result: dict) -> Path:
    """把产物**原文**落盘 —— 判定看不到的东西，人能看到。

    🔴 这是**唯一可靠的"完整性"真值来源**。
    实测（`docs/discussion-log.md` F1）：判定被 800 字预算截断，
    既会误判完整产物、又会漏掉真占位符（占位符正好落在被截掉的中段）。
    **只有把原文摊开给人看，"这份到底完不完整"才有答案。**

    落 `runs/<tag>/artifacts/<case>__r<rep>/`：每个产物一个文件 + 一份 `_index.md`。
    """
    from proto.artifacts import ARTIFACTS

    d = out_dir / "artifacts" / f"{case_id}__r{rep}"
    d.mkdir(parents=True, exist_ok=True)
    if not ARTIFACTS:
        (d / "_index.md").write_text("# （本次没有产物）\n", encoding="utf-8")
        return d

    from proto.artifacts import artifact_body

    rows = ["| 产物 | 标题 | 字数 | 类型 | 文件 |", "|---|---|---|---|---|"]
    for aid, a in ARTIFACTS.items():
        # 类型和文件名都取**记录里那份**（`store_artifact` 按声明的类型定好的）——
        # 这里另算一遍的话，会和它当场落盘时写的那份对不上。
        fn = a.get("file") or f"{aid}_{_slug(a['title'])}{a.get('ext', '.md')}"
        (d / fn).write_text(artifact_body(aid, a), encoding="utf-8")
        rows.append(
            f"| `{aid}` | {a['title']} | {len(a['content'])} "
            f"| {a.get('ext', '.md').lstrip('.')} | [{fn}]({fn}) |"
        )

    (d / "_index.md").write_text(
        f"# {case_id} · 第 {rep} 遍 · 产物原文\n\n"
        f"共 {len(ARTIFACTS)} 个，合计 {sum(len(v['content']) for v in ARTIFACTS.values())} 字。\n\n"
        + "\n".join(rows)
        + "\n\n> **这是判定看不到的原文** —— 判「完整性」以这里为准，不以判定的话为准。\n",
        encoding="utf-8",
    )
    return d


#: 平台回执的**固定开场白** → （记号, 这是什么）。这些措辞是 `proto/agent.py` 里写死的，
#: 按它判"这条回执属于哪种出口"，比让读者自己从一大段文字里猜可靠。
_RECEIPT_KINDS: tuple[tuple[str, str], ...] = (
    ("⚠️ 这次没完成，它在请求信息", "❓ **执行器在提问**（待答：平台会把这段转达给用户）"),
    ("⚠️ 执行器跑了", "⚠️ **执行器没做成**（既没完成、也没说缺什么）"),
    ("已交付", "✅ **交付回执**（执行器已写入产物库）"),
    ("没有能力「", "⛔ **被平台拒绝**：没有这个能力"),
    ("不存在，**这次没有派发**", "⛔ **被平台拒绝**：产物 ID 不存在"),
)

#: 工具返回的**来源**（读日志的人最需要知道的一条：这段字是谁写的）
_TOOL_SOURCE: dict[str, str] = {
    "delegate": "平台（`delegate` 的回执：**平台的框架话 + 执行器的原话**拼在一起）",
    "read_skill": "平台（技能原文 —— 主 agent 主动读的，渐进式披露第二层）",
    "read_artifact": "产物库（**产物正文全文**）",
    "write_artifact": "产物库（写入回执）",
    "list_artifacts": "产物库（清单，不含正文）",
}


#: 平台回执里的**分段标记** —— 拆开"平台的框架话"与"执行器的原话"。
#: 标记都是 `proto/agent.py` 里写死的措辞；`label` 是给读日志的人看的。
#:
#: 🔴 **加/改这里的标记，必须回 `proto/agent.py` 确认那句话还在** ——
#: 命不中的标记**不会报错**，只会静默失效。踩过：曾经有一条
#: `"\n（平台已替你查过"` 是照着**系统提示**（`_MAIN_PROMPT`）那句写的，
#: 而系统提示永远不会出现在工具返回里 —— 它从加进来那天起就没命中过任何东西
#: （2026-11-02 删）。**写标记要照着回执的实际措辞抄，不要凭印象。**
_RECEIPT_SPLIT: tuple[tuple[str, str], ...] = (
    ("执行器说：", "**执行器的原话**（它说了什么、在问什么）"),
    ("\n\n【已做到】", "平台附注 · 已做到什么程度"),
    ("\n\n【还缺】", "平台附注 · 还缺什么"),
    ("\n\n它最后说：", "**执行器的原话**（最后一轮）"),
    ("摘要：", "**执行器的一句话摘要**"),
    ("\n（平台已登记为待答", "平台给主 agent 的指示"),
    ("\n（你可以换个说法", "平台给主 agent 的指示"),
)


def _split_receipt(content: str) -> list[tuple[str, str]]:
    """把平台回执拆成「**谁写的** / 写了什么」若干段 —— 空列表表示没拆出来。

    用户要求：*"哪些是提问 来源是哪里"* —— 回执里混着平台的框架话和执行器的原话，
    不拆开就只能靠读者从一大段文字里猜哪句是谁说的。
    """
    marks: list[tuple[int, str, str]] = []
    for marker, label in _RECEIPT_SPLIT:
        i = content.find(marker)
        if i >= 0:
            marks.append((i, marker, label))
    if not marks:
        return []
    marks.sort()
    out: list[tuple[str, str]] = []
    if marks[0][0] > 0:
        out.append(("平台的框架话", content[: marks[0][0]].strip()))
    for j, (i, marker, label) in enumerate(marks):
        start = i + len(marker) if marker.endswith("：") else i
        end = marks[j + 1][0] if j + 1 < len(marks) else len(content)
        text = content[start:end].strip()
        if text:
            out.append((label, text))
    return out


def _turn_summary(turns: list[dict], i: int) -> list[str]:
    """每轮开头的小结 —— 回答"系统反问了吗 / 用户输入是谁给的 / 平台做了什么"。

    用户要求（2026-10-29）：*"skill 里面不是有提问嘛…它是否反问了 是否用户输入了
    是不是你模拟用户输入的"* —— **测试里的"用户"是 case YAML 里预置的字符串，
    不是真人即时输入**，这一点必须在日志里说清楚，否则读的人会以为看到了真实交互。
    """
    t = turns[i]
    prev = turns[i - 1] if i > 0 else None
    human = t.get("input_source") == "human"
    out = ["**本轮小结**", ""]
    if prev is None:
        out.append("- 上一轮：无（首轮）")
    else:
        waiting = [x for x in (prev.get("tasks") or []) if x.get("stage") == "waiting_for_input"]
        prev_lines = prev.get("log") or []
        relayed = [ln for ln in prev_lines if "finalize: 透传" in ln]
        if waiting:
            who = "、".join(f"`{x['id']}`（{x['skill']}）" for x in waiting)
            out.append(
                f"- 上一轮：系统**反问**了 —— {len(waiting)} 个 Task 在等答复：{who}"
                + ("；平台已**透传**给用户" if relayed else "；⚠️ 没看到透传")
            )
        else:
            out.append("- 上一轮：系统没有反问（没有 Task 等答复）")
    out.append(
        "- 本轮输入：**真人当场输入**（`evals.chat` 交互式跑，不是预置脚本）"
        if human
        else "- 本轮输入：**测试脚本预置**（`evals/cases/*.yaml` 的 `turns` 里写死的字符串）"
        "，**不是真人即时输入**"
    )
    own = (t.get("log") or [])[len(prev.get("log") or []) :] if prev else (t.get("log") or [])
    fed = [ln for ln in own if "把用户这条消息" in ln]
    if fed:
        out.append(f"- 平台动作：{fed[0].split('→')[-1].strip()}")
    elif prev is not None and any(x.get("stage") == "waiting_for_input" for x in (prev.get("tasks") or [])):
        out.append("- 平台动作：⚠️ 上一轮有待答，但本轮没看到投递记录")
    out.append("")
    return out


def _render_messages(turn: dict) -> list[str]:
    """把本轮消息渲染成**带来源与类型**的记录。

    用户要求（2026-10-29）：*"哪些是推理 哪些是正文 是哪个在推理 哪个在输出也不知道
    哪些是提问 来源是哪里 哪些是用户输入"* —— 所以每条都要能看出
    **谁说的（来源）+ 说的是什么性质（类型）**，不能只丢一个 `AIMessage` 类型名。
    """
    out: list[str] = []
    call_name: dict[str, str] = {}  # tool_call_id → 工具名（用来判返回的来源）
    call_args: dict[str, dict] = {}  # tool_call_id → 调用参数（"读了哪份"要靠它）
    pending: list[tuple[str, dict]] = []  # 还没配上返回的调用（老数据没有 id 时按顺序配）
    n = 0

    def _take(tid: object) -> tuple[str, dict]:
        """取这条返回对应的调用 —— 优先按 `tool_call_id` 精确配对，退化为按顺序。"""
        key = str(tid) if tid else ""
        if key and key in call_name:
            name = call_name.pop(key)
            args = call_args.pop(key, {})
            for i, (nm, _) in enumerate(pending):
                if nm == name:
                    pending.pop(i)
                    break
            return name, args
        return pending.pop(0) if pending else ("?", {})
    for m in turn.get("messages") or []:
        kind = m.get("kind", "?")
        content = str(m.get("content") or "")
        calls = m.get("tool_calls") or []
        if kind == "HumanMessage":
            continue  # 上面"用户输入"已经有了
        if kind == "AIMessage":
            if content.strip():
                n += 1
                out += [
                    f"**{n}. 🤖 主 agent 输出**（它自己说的话 —— 决定下一步做什么）",
                    "",
                    "```",
                    content.strip(),
                    "```",
                    "",
                ]
            for c in calls:
                n += 1
                args = c.get("args") or {}
                if c.get("id"):
                    call_name[str(c["id"])] = str(c.get("name"))
                    call_args[str(c["id"])] = args
                pending.append((str(c.get("name")), args))
                out += [
                    f"**{n}. 🔧 主 agent 调用工具 `{c.get('name')}`**"
                    + ("　→ **交接给执行器**" if c.get("name") == "delegate" else ""),
                    "",
                    "```",
                    *[f"{k} = {v!r}" for k, v in args.items()],
                    "```",
                    "",
                ]
            continue
        if kind == "ToolMessage":
            n += 1
            tool, target = _take(m.get("tool_call_id"))
            mark = next((tag for pre, tag in _RECEIPT_KINDS if content.startswith(pre)), "")
            out += [
                f"**{n}. ↩️ 返回给主 agent**（工具 `{tool}`）{mark}",
                "",
                f"<sub>来源：{_TOOL_SOURCE.get(tool, '平台')}</sub>",
                "",
            ]
            if tool in ("read_artifact", "read_skill"):
                # 🔴 正文/原文**不进日志**（用户要求：只写"读了啥、谁读的"）——
                # 产物落盘在 `art_*`，技能原文在 `evals/skills/`，都另有出处。
                what = "产物" if tool == "read_artifact" else "技能"
                key = "artifact_id" if tool == "read_artifact" else "skill_name"
                out += [
                    f"　**主 agent 读了{what} `{target.get(key, '?')}` 的全文**"
                    f"（{len(content)} 字，原文不在这里复述 —— "
                    + ("见同目录 `art_*` 落盘文件" if tool == "read_artifact" else "见 `evals/skills/`")
                    + "）",
                    "",
                ]
                continue
            parts = _split_receipt(content) if tool == "delegate" else []
            if parts:
                for label, text in parts:
                    # label 自带加粗记号，这里不要再包一层
                    out += [f"　{label}：", "", "```", text, "```", ""]
            else:
                out += [
                    "```",
                    content if len(content) <= 1200 else content[:1200] + f"\n…（共 {len(content)} 字）",
                    "```",
                    "",
                ]
            continue
        if kind == "SystemMessage":
            n += 1
            out += [
                f"**{n}. 📌 平台注入**（每轮开头自动塞给主 agent 的**任务台账** —— "
                "它靠这个知道已经派过什么、谁在等答复）",
                "",
                "```",
                content,
                "```",
                "",
            ]
            continue
        n += 1
        out += [f"**{n}. {kind}**", "", "```", content[:800], "```", ""]
    return out


def dump_run_record(out_dir: Path, case_id: str, rep: int, result: dict) -> None:
    """落盘**运行全记录**（一个 run 就这一份）—— 人工审计"过程对不对、交接了什么"。

    用户要求（2026-10-29）：*"不能遗漏任何关键分支 软硬判断尤为关键
    信息交接的地方 交接了什么也很关键 用户输入 ai输出 工具调用 诸如此类"*、
    *"日志你为啥要搞几个 放一起可以不"*。

    结构（后续由父进程追加）：① 概览 → ② 运行全记录（按轮）→ ③ 软判断留档（`judge.py` 追加）
    → ④ 原始 trace 附录（`run.py` 追加）。
    """
    d = out_dir / "artifacts" / f"{case_id}__r{rep}"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {case_id} · 第 {rep} 遍 · 运行全记录",
        "",
        "> 一份文件读完：**概览 → 逐轮全记录 → 软判断留档 → 原始 trace 附录**。",
        "",
        f"- 耗时 {result['wall_s']}s｜token {result['usage'].get('total_tokens', 0)}"
        f"｜LLM 调用 {result['usage'].get('llm_calls', 0)}",
        "",
    ]
    if result.get("error"):
        lines += ["## ⚠️ 整轮炸掉", "```", result["error"][:3000], "```", ""]
    for _i, t in enumerate(result["turns"]):
        lines += [
            f"## 第 {t['turn']} 轮",
            "",
            *_turn_summary(result["turns"], _i),
            "### 用户输入（**真人当场输入**）"
            if t.get("input_source") == "human"
            else "### 用户输入（**测试脚本预置**，不是真人即时输入）",
            "",
            f"> {t['user']}",
            "",
            f"<sub>主上下文 {t['main_ctx_chars']} 字（不含 system prompt）"
            f"｜产物库 {t['artifacts_chars']} 字｜delegate {len(t['delegate_calls'])} 次</sub>",
            "",
        ]
        warn = ctx_warn(t["main_ctx_chars"] + t.get("sys_prompt_chars", 0))
        if warn:
            lines += [f"> {warn}", ""]
        # ── 本轮全部消息，按发生顺序（用户输入 / 平台注入 / AI 输出 / 工具调用与返回）──
        lines += ["### 本轮消息（按发生顺序）", ""]
        lines += _render_messages(t)
        # 🔴 平台日志 —— 机制是否生效的直接证据（投递 / 透传 / 取代 / 注入 / 工具）
        if t.get("log"):
            lines += ["### 平台日志", "", "```"] + [f"  {x}" for x in t["log"]] + ["```", ""]
        if t["tasks"]:
            lines += [
                "### Task 台账",
                "",
                "| Task | 技能 | 状态 | **产出** | **输入引用** | 进度 / 还缺 |",
                "|---|---|---|---|---|---|",
            ]
            for x in t["tasks"]:
                note = "；".join(
                    y for y in [x.get("progress") or "", "缺：" + "、".join(x.get("missing") or []) if x.get("missing") else ""] if y
                )
                lines.append(
                    f"| `{x['id']}` | {x['skill']} | {x['stage']} "
                    f"| {', '.join(x.get('artifacts') or []) or '—'} "
                    f"| {', '.join(x.get('refs') or []) or '—'} | {note or '—'} |"
                )
            lines.append("")
            for x in t["tasks"]:
                if x.get("report"):
                    lines += [
                        f"<details><summary>{x['id']} 执行器原话（交接给主 agent 的就是它）</summary>",
                        "",
                        x["report"],
                        "",
                        "</details>",
                        "",
                    ]
    (d / "_run.md").write_text("\n".join(lines), encoding="utf-8")


