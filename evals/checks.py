"""状态断言 —— **能靠代码算的，不交给 LLM**（设计原则 ③）。

每个 check 是纯函数：`(run, turn, **args) -> (ok, detail)`。

- `run`：整场跑的完整记录（含 usage / 产物写入流水 / 错误）
- `turn`：**该轮结束时的快照**（见 `_child.py` 的 `snapshot()`）——
  case YAML 里的 `at: N` 决定取哪一轮，省略就是最后一轮

`CHECKS` 的 key 就是 case YAML 里 `check:` 的名字。
**detail 要能直接看懂**：失败时不用打开 JSON 就能判断是"机制坏了"还是"断言写错了"。
"""

from __future__ import annotations

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
    got = len(_tasks(turn))
    return got == n, f"task 数 = {got}（期望 {n}）：{_fmt_tasks(turn)}"


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
def check_missing_excludes(run: dict, turn: dict, words: list[str]) -> Result:
    """没了结的 task 的 `missing` 里不该出现这些词 —— 验 P1「不重复问」。

    ⚠️ **只扫 `missing`，不扫 `progress` / `report`** —— 那两个是自由散文，
    子串必然误伤。实测踩过：`progress` 里写"已收到**三年级**数学备课任务"，
    扫 "年级" 就命中了，把一次**正确**的行为判成了重复问。
    要判散文得用 `soft:` 的 LLM 评分。
    """
    bad: list[str] = []
    for t in _tasks(turn):
        if t.get("stage") not in ("waiting_for_input", "failed", "incomplete"):
            continue
        blob = " ".join(t.get("missing") or [])
        hit = [w for w in words if w in blob]
        if hit:
            bad.append(f"{t['id']}({t['skill']}) 又缺 {'/'.join(hit)}：{blob[:80]}")
    return not bad, "；".join(bad) if bad else f"没重复问 {words}"


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


@_reg
def check_artifact_writes_timing(run: dict, turn: dict, max_gap_s: float = 0.1) -> Result:
    """🔬 **测试有效性**：产物写入有没有真的**同时**发生。

    ⚠️ 实测教训：只数"有几个线程"是**不够**的 —— 第一次跑这条绿了，
    但看时间戳发现写入间隔 **4–205 秒**，竞态窗口在**微秒级**，等于根本没测到。
    并发 ≠ 多线程，得看时间。
    """
    w = run.get("artifact_writes") or []
    if len(w) < 2:
        return False, f"只有 {len(w)} 次写入，谈不上并发"
    ts = sorted(x["t"] for x in w)
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    gap = min(gaps)
    threads = len({x["thread"] for x in w})
    return gap <= max_gap_s, (
        f"{len(w)} 次写入 / {threads} 个线程，**最小间隔 {gap:.2f}s**（要 ≤{max_gap_s}s 才算真并发）"
    )


@_reg
def check_missing_includes(run: dict, turn: dict, words: list[str]) -> Result:
    """至少有一个没了结的 task 的 `missing` 里出现这些词 —— 验"确实在要信息"。"""
    blobs = [
        f"{t.get('progress', '')} {' '.join(t.get('missing') or [])}"
        for t in _tasks(turn)
        if t.get("stage") in ("waiting_for_input", "failed", "incomplete")
    ]
    blob = " ".join(blobs)
    missing = [w for w in words if w not in blob]
    return not missing, f"没找到 {'/'.join(missing)}；实际：{blob[:200] or '（空）'}"


# ──────────────────────────── 委派与兜底 ────────────────────────────


@_reg
def check_delegate_calls_max(run: dict, turn: dict, n: int) -> Result:
    """到该轮为止，主 agent 一共发了几次 `delegate` —— 验"不重复委派"。"""
    got = sum(len(t.get("delegate_calls") or []) for t in run["turns"][: turn["turn"]])
    return got <= n, f"delegate 调用 {got} 次（上限 {n}）"


@_reg
def check_delegate_calls_exact(run: dict, turn: dict, n: int) -> Result:
    got = sum(len(t.get("delegate_calls") or []) for t in run["turns"][: turn["turn"]])
    return got == n, f"delegate 调用 {got} 次（期望 {n}）"


@_reg
def check_platform_relayed(run: dict, turn: dict) -> Result:
    """**平台把待答透传给用户了** —— 这是 F7 修复的核心动作。

    2026-10-29 改：`finalize` 不再用 LLM 判断"主 agent 问没问"，
    **有待答就无条件原样贴出去**。这条断言就是在日志里抓这个动作。
    """
    hits = [ln for t in run["turns"][: turn["turn"]] for ln in t.get("log", []) if "finalize: 透传" in ln]
    return bool(hits), f"透传了：{hits}" if hits else "⚠️ 没看到透传 —— 待答可能没到用户手上"


@_reg
def check_no_platform_relay(run: dict, turn: dict) -> Result:
    hits = [ln for t in run["turns"][: turn["turn"]] for ln in t.get("log", []) if "finalize: 透传" in ln]
    return not hits, f"不该有透传，实际：{hits}" if hits else "没有待答，未透传"


@_reg
def check_platform_fed_answer(run: dict, turn: dict) -> Result:
    """**平台把用户原话投给待答任务了** —— 待答的解除走的是平台，不是主 agent 重派。"""
    hits = [
        ln
        for t in run["turns"][: turn["turn"]]
        for ln in t.get("log", [])
        if "把用户这条消息**原话**投过去" in ln
    ]
    return bool(hits), f"投递了：{hits}" if hits else "⚠️ 没看到投递"


@_reg
def check_finalize_rescued(run: dict, turn: dict) -> Result:
    """旧名保留（语义已变）：现在是"平台有没有透传"。"""
    return check_platform_relayed(run, turn)


@_reg
def check_no_finalize_rescue(run: dict, turn: dict) -> Result:
    """旧名保留（语义已变）：现在是"有没有产生待答"。"""
    return check_no_platform_relay(run, turn)


# ──────────────────────────── 隔离与体积 ────────────────────────────


@_reg
def check_main_agent_no_artifact_read(run: dict, turn: dict, allow: int = 0) -> Result:
    """**主 agent 不许把产物正文读进自己的上下文** —— 转交只需传 ID。

    比"主上下文体积上限"精确：体积会随它读技能原文、对话长短浮动，
    而这条直接判**它有没有读产物正文**（工具调用的参数里带着 `artifact_id`）。

    ⚠️ `allow` 留给"用户明确要求看内容"的场景；默认 0（一次都不该读）。
    """
    reads = [
        c
        for t in run["turns"][: turn["turn"]]
        for c in (t.get("tool_calls") or [])
        if c.get("name") == "read_artifact"
    ]
    if len(reads) <= allow:
        return True, f"没有读产物正文（{len(reads)} 次，上限 {allow}）"
    return False, f"读了 {len(reads)} 次产物正文：{[c.get('args') for c in reads]}"


@_reg
def check_main_ctx_under(run: dict, turn: dict, n: int) -> Result:
    """主 agent 上下文体积上限 —— 隔离效果的现场读数。"""
    got = turn.get("main_ctx_chars", 0)
    return got < n, f"主上下文 {got} 字（上限 {n}）"


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

#: **作者写的**占位符 —— 判定规则第 1 条说它们"不能视为完成"，这里用代码直接抓。
#: ⚠️ 只收**明确**的写法：单个「略」字不能收（"策略""省略"都会误伤）。
#: 技能模板里的原样残留（如「（活动名称）」）由 case 用 `words:` 追加 —— 模板是各技能自己的。
PLACEHOLDER_WORDS: tuple[str, ...] = (
    "待补充",
    "待填写",
    "待完善",
    "此处省略",
    "TODO",
    "TBD",
    "占位",
)


@_reg
def check_artifact_no_placeholder(run: dict, turn: dict, words: list[str] | None = None) -> Result:
    """产物正文里不许出现作者写的占位符 —— 交付完整性的代码侧读数。"""
    words = list(words) if words else list(PLACEHOLDER_WORDS)
    arts = run.get("artifact_final") or {}
    if not arts:
        return False, "没有任何产物"
    hits: list[str] = []
    for aid, a in arts.items():
        content = str(a.get("content") or "")
        for w in words:
            if w in content:
                hits.append(f"{aid} 含「{w}」")
    return (not hits), ("；".join(hits) if hits else f"{len(arts)} 份产物无占位符（扫 {len(words)} 个词）")


def strip_leading_comments(text: str) -> str:
    """剥掉开头的空白与 HTML 注释。

    技能契约明确允许「`<!DOCTYPE html>` 之前先放一段 `<!-- 设计说明 -->` 注释」，
    所以判"是不是 HTML 文档"必须先剥注释 —— 用"开头 N 字里找标记"这种近似窗口会误判：
    实测（`t2-refs5`）设计说明写了 36 行，`<!DOCTYPE html>` 落在 1000 字开外，
    产物完全合规却被判成"不是 HTML"。
    """
    s = text.lstrip()
    while s.startswith("<!--"):
        end = s.find("-->")
        if end == -1:
            break
        s = s[end + 3 :].lstrip()
    return s


def looks_like_html(text: str) -> bool:
    """剥掉前导注释后，正文是不是从 HTML 文档头开始。"""
    s = strip_leading_comments(text).lower()
    return s.startswith("<!doctype html") or s.startswith("<html")


@_reg
def check_artifact_looks_like_html(run: dict, turn: dict, min_count: int = 1) -> Result:
    """至少 `min_count` 份产物是**完整的 HTML 文档**（有头有尾）—— 验"网页"这种交付形态。"""
    arts = run.get("artifact_final") or {}
    ok_ids: list[str] = []
    bad: list[str] = []
    for aid, a in arts.items():
        content = str(a.get("content") or "")
        if looks_like_html(content) and "</html>" in content.lower():
            ok_ids.append(aid)
        elif looks_like_html(content):
            bad.append(f"{aid} 有开头但没有 </html>")
    if len(ok_ids) >= min_count:
        return True, f"{len(ok_ids)} 份完整 HTML：{ok_ids}；{'；'.join(bad)}"
    return False, f"完整 HTML 只有 {len(ok_ids)} 份（要 {min_count}）：{ok_ids}；{'；'.join(bad)}"


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


@_reg
def check_artifact_write_no_collision(run: dict, turn: dict) -> Result:
    """**每次 `store_artifact` 都要拿到唯一 ID**。

    `store_artifact` 用 `len(ARTIFACTS)+1` 生成 ID —— 主 agent 并行发多个 `delegate` 时
    ToolNode 是并发执行工具调用的，两个执行器同时算 `len+1` 会拿到**同一个 ID**，
    后者覆盖前者。写入次数 > 产物个数，就是撞了。
    """
    w = run.get("artifact_writes") or []
    ids = run.get("artifact_ids") or []
    dup = len(w) - len(ids)
    if dup <= 0:
        return True, f"{len(w)} 次写入 / {len(ids)} 个产物，无覆盖"
    by_id: dict[str, list] = {}
    for x in w:
        by_id.setdefault(x["aid"], []).append(x)
    clash = {k: [x["title"] for x in v] for k, v in by_id.items() if len(v) > 1}
    return False, f"⚠️ {dup} 次写入被覆盖：{clash}"


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
def check_no_exception(run: dict, turn: dict) -> Result:
    err = run.get("error")
    return not err, f"⚠️ 整轮炸了：{err[:300]}" if err else "无异常"


@_reg
def check_fault_fired(run: dict, turn: dict) -> Result:
    """**测试有效性断言**：注入的故障真的发生了。"""
    f = run.get("fault") or {}
    return f.get("fired", 0) > 0, f"注入触发 {f.get('fired', 0)} 次"


@_reg
def check_run_task_count_over(run: dict, turn: dict, skill: str, n: int) -> Result:
    """🔬 **测试有效性**：这个技能真的被跑了几次（首跑 + 每次投递各算一次）。

    用来判"这条 case 有没有压到重入路径"：如果执行器一上来把两个问题一起问了，
    就只会有 1 次 `run_task` —— 那条"用户分两次答"的回归**根本没被触发**，
    `task_stages: completed` 绿了也不算数。
    """
    got = sum(1 for x in (run.get("run_tasks") or []) if x.get("skill") == skill)
    return got >= n, f"{skill} 跑了 {got} 次（要 ≥{n} 才算压到重入路径）"


@_reg
def check_llm_calls_under(run: dict, turn: dict, n: int) -> Result:
    got = (run.get("usage") or {}).get("llm_calls", 0)
    return got <= n, f"LLM 调用 {got} 次（上限 {n}）"


# ──────────────────────────── 回复文本 ────────────────────────────


@_reg
def check_reply_contains(run: dict, turn: dict, subs: list[str]) -> Result:
    r = turn.get("reply") or ""
    miss = [s for s in subs if s not in r]
    return not miss, f"回复里没找到 {miss}｜回复：{r[:200]}"


@_reg
def check_reply_excludes(run: dict, turn: dict, subs: list[str]) -> Result:
    r = turn.get("reply") or ""
    hit = [s for s in subs if s in r]
    return not hit, f"回复里出现了 {hit}｜回复：{r[:200]}" if hit else f"回复不含 {subs}"


@_reg
def check_reply_nonempty(run: dict, turn: dict) -> Result:
    r = (turn.get("reply") or "").strip()
    return bool(r), f"回复 {len(r)} 字" if r else "⚠️ 主 agent 一个字没说"


@_reg
def check_no_duplicate_completed(run: dict, turn: dict, skills: list[str] | None = None) -> Result:
    """同一个技能**最多只能有一个 `completed` 的 Task** —— 这才是 S14「不重复委派」。

    ⚠️ 别用 `delegate_calls_max` 代替：**补救性的重派是合法的**
    （执行器没按指令落库 → 判定 incomplete → 主 agent 换个说法重派 → 这次成了）。
    实测踩过：`s01b` r1 用了 4 次委派完成 3 件事，那是**恢复路径生效**，不是缺陷。
    真正的缺陷是"同一件事被做成了两份成品"。
    """
    skills = skills or sorted({t["skill"] for t in _tasks(turn)})
    bad: list[str] = []
    for s in skills:
        done = [t["id"] for t in _by_skill(turn, s) if t["stage"] == "completed"]
        if len(done) > 1:
            bad.append(f"{s} 有 {len(done)} 个 completed：{done}")
    return not bad, "；".join(bad) if bad else f"每个技能最多一个 completed（{len(skills)} 个技能）"


@_reg
def check_supersede_worked(run: dict, turn: dict, skill: str) -> Result:
    """重新委派同一技能后，旧的待答必须被取代、且不能还挂着等答复（S19）。"""
    ts = _by_skill(turn, skill)
    n_sup = sum(1 for t in ts if t["stage"] == "superseded")
    n_wait = sum(1 for t in ts if t["stage"] == "waiting_for_input")
    ok = n_sup >= 1 and n_wait == 0
    return ok, f"superseded={n_sup} waiting={n_wait}｜{_fmt_stages(turn, skill)}"
