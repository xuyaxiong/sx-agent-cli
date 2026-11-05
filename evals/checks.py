"""状态断言 —— **能靠代码算的，不交给 LLM**（设计原则 ③）。

每个 check 是纯函数：`(run, turn, **args) -> (ok, detail)`。

- `run`：整场跑的完整记录（含 usage / 产物写入流水 / 错误）
- `turn`：**该轮结束时的快照**（见 `_child.py` 的 `snapshot()`）——
  case YAML 里的 `at: N` 决定取哪一轮，省略就是最后一轮

`CHECKS` 的 key 就是 case YAML 里 `check:` 的名字。
**detail 要能直接看懂**：失败时不用打开 JSON 就能判断是"机制坏了"还是"断言写错了"。

## 🔴 写断言前先问：**这条断的是「谁的行为」？**（2026-11-02）

模型是**概率**的 —— 断"模型走了几步、输出多长"的断言**必然脆**，而且 flake 时
**说不清是系统坏了、还是模型今天走了另一条路**（用户原话：
*"你经常断言一些数量上的东西……有没有想过这些断言**极其脆弱**？大语言模型本就是**基于概率**的呀"*）。

| 断谁 | 判 | 例 |
|---|---|---|
| **平台机制**（代码） | ✅ **稳** | `platform_relayed` / `platform_fed_answer` · `artifact_write_no_collision` · `artifact_no_cross_attribution` |
| **产物性质** | ✅ **稳** | `artifact_no_placeholder` · `artifact_html_complete` |
| **不变量**（"不该有" / "至多一个" / "每个都有"） | ✅ **稳** | `task_count: 0` · `no_duplicate_completed` · `every_task_has_artifact` |
| **目标状态**（断结果，不断路径） | ✅ 大体稳 | `task_stages`（某技能**最终** completed） |
| 🔴 **模型走了几步 / 输出多长** | **脆** | `delegate_calls_max` · `task_count_at_least` · `llm_calls_under` · `reply_chars_under` · `artifact_chars_over` |

**脆断言不是不能写，是不能当回归哨兵**：它们红了先怀疑采样，别急着改代码。
**真要压某条路径，让平台把路造出来** —— 先例是 `fault.structured` 故障注入；
case 也可以用 `initial_tasks` 直接注入一个"待答"的 Task，
于是"答复投递（重入）"那条路**从第 1 轮起就必然被走到**（见 `_child.py`）。
"""

from __future__ import annotations

from html.parser import HTMLParser  # 核 HTML 骨架（不在正文里找标记）

from typing import Any, Callable

Result = tuple[bool, str]
Check = Callable[..., Result]

CHECKS: dict[str, Check] = {}


def _reg(fn: Check) -> Check:
    CHECKS[fn.__name__.removeprefix("check_")] = fn
    return fn


# ──────────────────────────── 小工具 ────────────────────────────


def _tasks(turn: dict) -> list[dict]:
    return turn.get("tasks") or []


def _by_skill(turn: dict, skill: str) -> list[dict]:
    return [t for t in _tasks(turn) if t.get("skill") == skill]


def _fmt_tasks(turn: dict) -> str:
    ts = _tasks(turn)
    if not ts:
        return "（无）"
    return "；".join(f"{t['id']} {t['skill']}/{t['stage']}" for t in ts)


def _fmt_stages(turn: dict, skill: str) -> str:
    ts = _by_skill(turn, skill)
    if not ts:
        return "（无此 skill 的 task）"
    return "、".join(f"{t['id']}={t['stage']}" for t in ts)


def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else [v]


# ──────────────────────────── 任务台账 ────────────────────────────


@_reg
def check_task_count(run: dict, turn: dict, n: int) -> Result:
    """task 数**精确等于** n。

    ⚠️ **只有 `n=0` 该用它** —— "闲聊不该建 Task"是不变量。
    `n>0` 是**中段值**：派几个任务是模型的自由选择，会 flake；
    要断"至少发生了 N 路工作"请用 `task_count_at_least`。
    """
    got = len(_tasks(turn))
    return got == n, f"task 数 = {got}（期望 {n}）：{_fmt_tasks(turn)}"


@_reg
def check_task_count_at_least(run: dict, turn: dict, n: int) -> Result:
    """task 数 **≥ n** —— 下界，用来压"至少发生了 N 路工作"（并发测试那类）。

    ⚠️ 它断的**仍然是模型的行为**（派几个由它定），但下界比精确值稳得多：
    模型多派一个不算坏事，**少派才说明这条 case 没被压到**。
    """
    got = len(_tasks(turn))
    return got >= n, f"task 数 = {got}（要 ≥{n}）：{_fmt_tasks(turn)}"


@_reg
def check_task_stages(run: dict, turn: dict, stages: dict) -> Result:
    """`{skill: stage|[...]}` —— 每个 skill **至少有一个** task 命中给定状态。"""
    bad: list[str] = []
    for skill, want in stages.items():
        want = _as_list(want)
        if not any(t["stage"] in want for t in _by_skill(turn, skill)):
            bad.append(f"{skill} 期望 {'/'.join(want)}，实际 {_fmt_stages(turn, skill)}")
    return not bad, "；".join(bad) if bad else f"全部命中：{stages}"


@_reg
def check_no_task_stage(run: dict, turn: dict, stages: dict) -> Result:
    """`{skill: [stage]}` —— 该 skill 下**不允许**存在这些状态。"""
    bad: list[str] = []
    for skill, banned in stages.items():
        banned = _as_list(banned)
        hit = [t for t in _by_skill(turn, skill) if t["stage"] in banned]
        if hit:
            bad.append(f"{skill} 不该有 {'/'.join(banned)}，实际 {_fmt_stages(turn, skill)}")
    return not bad, "；".join(bad) if bad else f"干净：{stages}"


@_reg
def check_all_tasks_given_contain(run: dict, turn: dict, subs: list[str]) -> Result:
    """每个 task 的 `task` 文本都要含这些子串 —— 验"派发前就收齐了信息"。"""
    bad = [
        f"{t['id']}({t['skill']}) 缺 {'/'.join(s for s in subs if s not in t.get('task', ''))}"
        for t in _tasks(turn)
        if any(s not in t.get("task", "") for s in subs)
    ]
    return not bad, "；".join(bad) if bad else f"{len(_tasks(turn))} 个 task 都含 {subs}"



@_reg
def check_every_task_has_artifact(run: dict, turn: dict) -> Result:
    """每个 task 都要拿到**自己的**产物 —— 也是「failed 分支丢 artifacts」的探测器。

    实测踩过：`delegate` 的 failed 分支没写 `outcome.artifacts`，
    执行器明明产出了 3 个产物，台账里却是 `artifacts: []`。
    """
    ts = _tasks(turn)
    if not ts:
        return False, "一个 task 都没有"
    empty = [f"{t['id']}({t['skill']}/{t['stage']})" for t in ts if not t.get("artifacts")]
    return not empty, f"这些 task 没有产物：{empty}" if empty else f"{len(ts)} 个 task 都有产物"


@_reg
def check_artifact_no_cross_attribution(run: dict, turn: dict) -> Result:
    """**每个 task 记的产物，必须是它自己那几次 run 写出来的**（不许把读来的算成产出的）。

    🔴 旧版按**线程**归属，实测**空转**：产物写入发生在 langgraph 的工具线程里，
    和 `run_task` 的线程对不上（实测两边线程号完全不重合）→ owner 恒为 `None`
    → **永远不报**。它一直在给假绿。
    已改成按「每次 run 前后产物库的**差集**」归属（`_child.rec_run_task` 记的 `produced`）。

    ⚠️ 已知窗口：并行 run 的差集可能互相覆盖，会**漏报**（不会误报）。
    """
    produced_by_skill: dict[str, set[str]] = {}
    for x in run.get("run_tasks") or []:
        produced_by_skill.setdefault(str(x["skill"]), set()).update(x.get("produced") or [])
    bad: list[str] = []
    for t in _tasks(turn):
        own = produced_by_skill.get(t["skill"], set())
        extra = [a for a in (t.get("artifacts") or []) if a not in own]
        if extra:
            bad.append(f"{t['id']}({t['skill']}) 记了不是它写的产物：{extra}")
    return not bad, "；".join(bad) if bad else "没有串台"


# ⚠️ ~~`check_artifact_writes_timing`~~ **2026-11-05 删**（零 case 用）：
#    它断"两次写入的最小间隔 ≤ `max_gap_s`（默认 0.1 秒）"＝"真并发" —— 而
#    `e13-concurrent-artifacts` 的注释里记着**实测写入间隔 4–205 秒** ✗：
#    阈值跟现实对不上，挂上去只会红 ✗。"是不是并发写的"由
#    `artifact_writes_multithreaded`（>1 个线程 ✓）压着就够了 ✓。
@_reg
def check_delegate_calls_max(run: dict, turn: dict, n: int) -> Result:
    """到该轮为止，主 agent 一共发了几次 `delegate` —— 验"不重复委派"。

    ⚠️ **上界**：断的是模型的行为，会 flake。但方向是合理的 ——
    少派不算坏事（"该不该派"由 `task_stages` 兜），**多派才是 bug**（S14 重复委派）。

    📌 **原来还有一条 `delegate_calls_exact`（精确次数），2026-11-02 删了** ——
    "正好派 1 次"是中段值，模型不派（0 次）时它反而**不报**，
    而"该派没派"该由 `task_stages` 报。上界 + 目标状态两条合起来覆盖更全、也更稳。
    """
    got = sum(len(t.get("delegate_calls") or []) for t in run["turns"][: turn["turn"]])
    return got <= n, f"delegate 调用 {got} 次（上限 {n}）"


def _actions(run: dict, turn: dict, did: str) -> list[dict]:
    """到这一轮为止，平台做过哪些 `did` 动作 —— **读结构化字段**（`State.actions`）。

    ⚠️ 别退回"在 `turn["log"]` 里找那句话"：那是渲染产物，措辞一改断言就静默给错结论。
    """
    return [
        a
        for t in run["turns"][: turn["turn"]]
        for a in (t.get("actions") or [])
        if a.get("did") == did
    ]


@_reg
def check_platform_relayed(run: dict, turn: dict) -> Result:
    """**平台把待答透传给用户了** —— 这是 F7 修复的核心动作。

    2026-10-29 改：`finalize` 不再用 LLM 判断"主 agent 问没问"，
    **有待答就无条件原样贴出去**。

    🔴 **读 `actions`（对象），不读 `log`（渲染好的句子）**（2026-11-04）：
    原来这里是 `if "finalize: 透传" in ln` —— 改一句文案，这条断言就**静默**报
    "没看到透传"（判据一：渲染产物不许被反解析回结构）。
    """
    hits = _actions(run, turn, "relay")
    return bool(hits), f"透传了：{hits}" if hits else "⚠️ 没看到透传 —— 待答可能没到用户手上"


@_reg
def check_no_platform_relay(run: dict, turn: dict) -> Result:
    hits = _actions(run, turn, "relay")
    return not hits, f"不该有透传，实际：{hits}" if hits else "没有待答，未透传"


@_reg
def check_platform_fed_answer(run: dict, turn: dict) -> Result:
    """**平台把用户原话投给待答任务了** —— 待答的解除走的是平台，不是主 agent 重派。

    同 `check_platform_relayed`：读 `actions`，不读日志串。
    """
    hits = _actions(run, turn, "feed")
    return bool(hits), f"投递了：{hits}" if hits else "⚠️ 没看到投递"


# ⚠️ ~~`check_finalize_rescued`~~ / ~~`check_no_finalize_rescue`~~ **2026-11-05 删**：
#    它们是 `platform_relayed` / `no_platform_relay` 的**旧名别名** ✗（零 case 用）——
#    同一个语义两处定义，就是"一套，别分叉"要防的 ✓ 删别名，留正主。
@_reg
def check_reply_chars_under(run: dict, turn: dict, n: int) -> Result:
    got = len(turn.get("reply") or "")
    return got < n, f"回复 {got} 字（上限 {n}）"


@_reg
def check_artifact_chars_over(run: dict, turn: dict, n: int) -> Result:
    """产物库正文总量下限 —— **验隔离必须用长内容**，短内容测不出来。"""
    got = turn.get("artifacts_chars", 0)
    return got > n, f"产物库正文 {got} 字（下限 {n}）"


@_reg
def check_artifact_count(run: dict, turn: dict, n: int) -> Result:
    got = len(turn.get("artifacts") or {})
    return got == n, f"产物 {got} 个（期望 {n}）：{list((turn.get('artifacts') or {}).keys())}"


# ──────────────────────────── 产物正文（交付完整性） ────────────────────────────

@_reg
def check_artifact_html_complete(run: dict, turn: dict, min_count: int = 1) -> Result:
    """**声明为 `html` 的产物**要"有头有尾"（文档头开头、`</html>` 结尾）—— 验"网页"这种交付形态。

    🔴 **类型以声明为准，不嗅探**（用户 2026-10-30：*"就是不用嗅探呀 测试也不用"*）：
    哪些产物算"网页"，看它交付时**声明的 `file_type`**（`proto.artifacts.file_ext` 那张表，
    和落盘派发用的是同一张），**不看内容像不像**。
    这里只做一件事：核对"声明了 html 的那几份，正文是不是真的完整"。

    > 判据的差别：不是"看着像 HTML 就算网页"，而是"**你说了是网页，那就得是完整的网页**"。

    🔴 **用解析器看文档骨架，不在正文里找字符串**（2026-11-05）：
    原来是 `content.startswith("<!doctype html")` + `"</html>" in content` ✗ ——
    那是在**正文里找标记**：正文里**引用**了这三个字的地方（讲 HTML 的正文、代码块）也会命中 ✗，
    而"前面先放一段设计说明注释"又得靠 `_strip_leading_comments` 那种剥字符串的 hack 去绕 ✗。
    现在交给 `html.parser`：它把文档**拆成 token**（doctype 声明 / 标签 ✓），
    标记是不是"真在文档骨架的位置上"由**解析结果**说了算，不由"出现过没有"说了算 ✓。
    """
    from proto.artifacts import file_ext

    arts = run.get("artifact_final") or {}
    html_ids = [aid for aid, a in arts.items() if file_ext(a.get("type") or "") == ".html"]
    if not html_ids:
        return False, f"没有一份产物声明为 html（共 {len(arts)} 份：{list(arts)}）"
    ok_ids: list[str] = []
    bad: list[str] = []
    for aid in html_ids:
        shape = _doc_shape(str(arts[aid].get("content") or ""))
        if not shape["doctype"]:
            bad.append(f"{aid} 声明 html，但没有 `<!DOCTYPE>` 声明")
        elif not (shape["html_open"] and shape["html_close"]):
            bad.append(f"{aid} 声明 html，但文档骨架不全（<html> / </html> 没配齐）")
        else:
            ok_ids.append(aid)
    if len(ok_ids) >= min_count:
        return True, f"{len(ok_ids)} 份完整 HTML：{ok_ids}；{'；'.join(bad)}"
    return False, f"完整 HTML 只有 {len(ok_ids)} 份（要 {min_count}）：{ok_ids}；{'；'.join(bad)}"


class _DocShape(HTMLParser):  # noqa: D101 —— 只用来收骨架标记，见 `_doc_shape`
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.doctype = False
        self.html_open = False
        self.html_close = False

    def handle_decl(self, decl: str) -> None:
        # 解析器给的**是一个声明 token**（"DOCTYPE html"），不是"正文里有没有这几个字" ✓
        if decl.strip().lower().startswith("doctype"):
            self.doctype = True

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag == "html":
            self.html_open = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "html":
            self.html_close = True


def _doc_shape(content: str) -> dict[str, bool]:
    """把 HTML 拆成 token，只看**文档骨架**在不在（doctype / `<html>` 起止）。"""
    p = _DocShape()
    try:
        p.feed(content)
        p.close()
    except Exception:  # noqa: BLE001 —— 解析器对畸形输入也会抛；抛了就是骨架不全
        pass
    return {"doctype": p.doctype, "html_open": p.html_open, "html_close": p.html_close}


@_reg
def check_artifact_titles_contain(run: dict, turn: dict, subs: list[str]) -> Result:
    """产物标题里要能找到这些字样 —— 验"用户要的那几样都交付了"。"""
    titles = [str(a.get("title") or "") for a in (run.get("artifact_final") or {}).values()]
    miss = [s for s in subs if not any(s in t for t in titles)]
    return (not miss), (f"标题里没找到 {miss}｜实际：{titles}" if miss else f"标题齐：{titles}")


@_reg
def check_task_refs_contain(run: dict, turn: dict, skills: list[str]) -> Result:
    """这些技能的 task 必须带**产物引用**（`delegate(..., artifacts=[...])`）。

    依赖是**结构化传的**：主 agent 给 ID，执行器自己读全文。
    所以这条能当硬断言 —— 以前只能靠 LLM 评分判"task 文本里有没有转述内容"，
    而那个口径在新机制下**正好是反的**（转述正文才是错的）。
    """
    bad: list[str] = []
    for skill in skills:
        ts = _by_skill(turn, skill)
        if not ts:
            bad.append(f"{skill} 没有 task")
        elif not any(t.get("refs") for t in ts):
            bad.append(f"{skill} 的 task 没带 refs")
    return (not bad), ("；".join(bad) if bad else f"都带了产物引用：{skills}")


# ──────────────────────────── 产物写入流水（并发竞态） ────────────────────────────


# ⚠️ ~~`check_artifact_write_no_collision`~~ **2026-11-05 删**（零 case 用）：
#    它断"每次 `store_artifact` 拿到唯一 ID"—— 而那个竞态**端到端测不到**
#    （实测写入间隔 4–205 秒，窗口在微秒级 ✗）。**已经由 `unit_artifact_race.py` 压着** ✓
#    （那边还带"对照组必须炸"的有效性自检 ✓）—— 留在这里是**第二份定义** ✗。
@_reg
def check_artifact_writes_multithreaded(run: dict, turn: dict) -> Result:
    """**测试有效性断言**：产物确实是并发写的。

    如果所有写入都在同一个线程，说明这次跑**根本没触发并发** ——
    上面那条"没撞车"就是假阴性，E13 的结论无效。
    """
    w = run.get("artifact_writes") or []
    threads = {x["thread"] for x in w}
    return len(threads) > 1, f"{len(w)} 次写入来自 {len(threads)} 个线程：{sorted(threads)}"


# ──────────────────────────── 异常与注入 ────────────────────────────


@_reg
def check_task_faulted(run: dict, turn: dict, skill: str = "") -> Result:
    """**平台故障落进了 `Task`**（`Task.fault` 非空）—— 台账据此说真话（2026-11-05 修 A3）。

    ⚠️ 断的是**平台写进对象里的那个字段**，不是"回执/台账那句话说得对不对" ——
    文案怎么改都不影响这条（判据：断言读对象，不读渲染串）。
    """
    ts = _by_skill(turn, skill) if skill else _tasks(turn)
    hit = [t["id"] for t in ts if str(t.get("fault") or "").strip()]
    detail = (f"{hit} 记着平台故障" if hit
              else f"没有任何 Task 记着 fault（{'、'.join(t['id'] + '=' + str(t.get('stage')) for t in ts)}）")
    return bool(hit), detail


@_reg
def check_no_exception(run: dict, turn: dict) -> Result:
    err = run.get("error")
    return not err, f"⚠️ 整轮炸了：{err[:300]}" if err else "无异常"


@_reg
def check_fault_fired(run: dict, turn: dict) -> Result:
    """**测试有效性断言**：注入的故障真的发生了。

    ⚠️ **两种注入都要认**（2026-11-05 修）：`fault.structured`（`safe_structured` 那条路）
    与 `fault.stream`（流式那条路）**各有各的计数器** —— 原来只读前者，于是用 `stream`
    注入的 case（`e12-fault-stream`）**故障明明发生了，这条却报"触发 0 次"** ✗
    （实测：`results.json` 里 `Task.fault` 已经写着 `TimeoutError: （注入）流式调用抖动`，
    这条却红着 —— **一条把"有效"报成"无效"的断言，比没有更坏**：它会让人去查一个没坏的东西）。
    """
    st = run.get("fault") or {}
    sr = run.get("stream_fault") or {}
    n = int(st.get("fired", 0)) + int(sr.get("fired", 0))
    return n > 0, (f"注入触发 {n} 次"
                   f"（structured {st.get('fired', 0)} ＋ stream {sr.get('fired', 0)}）")


# ⚠️ ~~`check_run_task_count_over`~~ **2026-11-05 删**（零 case 用）：
#    它**自己的 docstring 就写着它脆** ✗ —— *"它断的是'模型会不会重入'，那是模型的
#    自由选择"*，而"正解是让平台造路（`initial_tasks` 注入一个待答 Task）" ✓。
#    既然正解已经落地（`t3` / `e-late-answer` / `s08` / `s19` 都在注入 ✓），
#    这条**脆的旧工具**就该退场 —— 留着只会有人再拿它写脆 case ✗。
