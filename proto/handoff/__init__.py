"""**交接** —— 每一跳的 schema 与模板，一处一个单元。

    proto/handoff/
      __init__.py    ← 本文件：**共享词汇表**（段名 / 来源标注 / 出口分类）
      briefing.py    ← 委托单（平台 → 执行器）：Task/Turn/QA schema ＋ render_briefing
      receipt.py     ← 回执（执行器 → 主 agent）：Receipt schema ＋ render_receipt

> 🔴 **用户 2026-11-03 定的组织方式**：*"各个地方的交接都用一个 schema 声明各处交接了什么内容，
> 然后**交接模板跟各自 schema 放在一起** —— 这样符合高内聚低耦合。"*
> 所以：**一个交接点 = 一个模块 = schema ＋ 模板**，改一处就够。

## 为什么共享词汇表要单独一份（不能跟着各模块各写一份）

这正是本项目踩过的坑：同一个语义（"判定说已完成什么"）原来在**五处**有五种写法 ——
交接书 `- **判定说**已做到：`、回执 `【已做到】（**判定说**）`、台账 `　　　已做到：`、
推回去 `**你已经完成了**：`、试答 `【已做到】`。**没有任何地方承载"它们该对齐"这条规矩。**

后果：`evals/harness` 手抄的一组分段标记，在 2026-11-02 回执改格式后
**新格式 0/8 命中**（旧格式 38/38），**一声不吭**地把 12560 字的回执切成 1200。

所以段名**只有这一份**：渲染器从这里取，记录层也从这里取 —— 失配结构上不可能。

## 🔴 一条更硬的规矩（用户 2026-11-03）

> *"**禁止你反结构化任何 llm 正文输出** —— 结构化处理的前提一定是 llm 输出本身就是结构化的。"*

**不许**把渲染成散文的文本再解析回结构。结构一旦渲染成散文就**回不去**了：
段名/来源标注/自由文本混在一行，自由文本里可以出现任何东西，任何措辞变更对解析器**不可见**。
正解是：**先有对象，注入上下文那一刻才渲染**；对象搭同一趟车
（`ToolMessage.additional_kwargs` —— 实测**不会漏给模型**）。没有对象就**如实显示成一段散文**。
"""

from __future__ import annotations

# ──────────────────────────── 段名 ────────────────────────────

#: **段名** —— 渲染器从这里取，记录层也从这里切。**这是唯一的来源。**
#:
#: ⚠️ 段名一律是 `【…】` 形式、**独占行首** —— 记录层靠这两条做确定性切分。
SECTION: dict[str, str] = {
    # ── 委托单（平台 → 执行器，`briefing.py`）──
    "task": "【任务】",
    "user_turns": "【用户原话】",
    "last_round": "【你上次说到哪】",
    "answered": "【已经答复过的信息】",
    "already_produced": "【已经产出的产物】",
    "readable": "【可读产物】",
    "resume": "【续跑提示】",
    # ── 回执（执行器 → 主 agent，`receipt.py`）──
    "deliver": "【交付】",
    "produced": "【产出的产物】",
    "report": "【执行器说】",
    "progress": "【已做到】",
    "missing": "【还缺】",
    "agent_answer": "【你补的信息】",
    "next": "【下一步】",
    # ── 派发被拒（平台 → 主 agent，**不建 Task**）──
    "not_dispatched": "【这次没有派发】",
    "available_skills": "【可用的能力】",
    "available_turns": "【本会话的用户原话】",
    "available_artifacts": "【本会话现有产物】",
}

#: **来源标注** —— 紧跟段名、写在**同一行**的那一小段（"这句是谁说的"）。
#:
#: 🔴 它**属于结构**，不是措辞装饰：记录层切段时要**原样剥掉**它，剩下的才是内容。
#: 没有这张表就只能靠"行首是 `（…）` 就剥"这种启发式 —— 而 `（无）` 这种**真内容**
#: 会被误剥（2026-11-03 实测踩到：派发被拒那条回执切出来少了一段）。
SOURCE_NOTE: dict[str, str] = {
    "report": "（**它说的** —— 原样，未经改写）",
    "progress": "（**判定说**）",
    "missing": "（**判定说**）",
    "agent_answer": "（**你说的** —— 已成对记进 Task，下次重入还会带上）",
}


#: 段名后面**怎么接内容** —— `inline` 同一行，`block` 换行再写。
#:
#: 🔴 这不是装饰：它决定渲染出来的**字节**，而记录层和渲染器必须看到同一份东西。
#: 没列出的段一律按 `block`。
SECTION_LAYOUT: dict[str, str] = {
    "deliver": "inline",
    "progress": "inline",
    "next": "inline",
    "not_dispatched": "inline",
    "available_skills": "inline",
    "available_turns": "inline",
    "available_artifacts": "inline",
}


# ──────────────────────────── 平台注入的标记 ────────────────────────────

#: **谁把这条消息塞进来的** —— 挂在 `additional_kwargs["injected_by"]` 上
#: （实测**不会漏给模型**，和回执对象同一条路）。
#:
#: 🔴 **为什么需要它**：`node_finalize` 把待答**透传**给用户时注入的是一条 `AIMessage` ——
#: 内容**是执行器的原话**，但记录层对所有 `AIMessage` 一视同仁，把它标成了
#: "🤖 主 agent 输出（它自己说的话）"。**来源标注在这一跳断了**（2026-11-03 实测）。
#: 正解不是"看内容像不像执行器的话"（那是反结构化），是**注入时打个标记**。
INJECTED: dict[str, str] = {
    "platform.finalize": "平台**透传**（执行器的话，原样转达给用户 —— **不是主 agent 说的**）",
}


# ──────────────────────────── 出口分类 ────────────────────────────

#: 【交付】那一行的**四种头**（`{}` 是变量）。
#:
#: 渲染器按它拼，记录层按它的**固定前缀**分类（`delivery_key`）——
#: 同一张表推出来的，所以"改了头忘了改分类"不会发生。
DELIVERY: dict[str, str] = {
    "completed": "✅ 已完成 —— 交付 {} 个产物",
    "completed_in_reply": "✅ 已完成 —— 成果在执行器的回复里（**没有产物**）",
    "waiting": "⏳ 没完成 —— **它在请求信息**",
    "failed": "❌ 失败 —— 推了 {} 轮，仍然既没完成、也没说明需要什么信息",
}


def delivery_key(head: str) -> str | None:
    """【交付】那一行的内容 → 它是哪种出口。**从 `DELIVERY` 推，不另写一份前缀。**

    取模板里 `{}` 之前的那一段当判据 —— 变量在后面，前缀是固定的。
    """
    for key, tpl in DELIVERY.items():
        if head.startswith(tpl.split("{}")[0]):
            return key
    return None


# ──────────────────────────── 每一跳用哪几段 ────────────────────────────

#: **声明**：这一跳会渲染哪几段。记录层和单测据此校验。
#:
#: ⚠️ 回执的段是**有条件**的（有产物才出【产出的产物】、试答过才出【你补的信息】），
#: 所以校验是 **⊆**（渲染出来的段名必须都在声明里），不是相等 ——
#: **多出一段没声明的就红**，那正是"加了段忘了登记"。
HOPS: dict[str, tuple[str, ...]] = {
    "briefing": (
        "task", "user_turns", "last_round", "answered",
        "already_produced", "readable", "resume",
    ),
    "receipt": (
        "deliver", "produced", "report", "progress", "missing", "agent_answer", "next",
    ),
    "receipt_rejected": (
        "not_dispatched", "available_skills", "available_turns", "available_artifacts",
    ),
}
