"""**图** —— 主 agent 一次回合：feed（投递原话）→ prepare（注入台账）→ agent（主 agent 循环）
→ finalize（兜底补问）。

## 这个文件是什么

整条链路的**装配处**：`build()` 把五个节点拼成一张图，两个 schema（`State` / `AgentState`）
与三个纯函数（`_feed_waiting` / `render_relay` / `next_pending`）都在这里。

**主 agent 是完整的 agent**，流程由它自己驱动。平台在旁边只做三件事：

  - **投递**（feed）：用户这条消息标了 `answer_to`（通道填的）就投给那**一个**待答任务
    —— 里面**同步跑执行器**（60–120 秒），所以**单起一个节点**〔沿革 29〕
  - **注入**（prepare）：把**任务台账**（谁在干什么、干到哪了、还缺什么）摆到它面前
    —— **只提供事实，不替它做决定**（"用户是不是在回答"是理解问题，该柔性）
  - **兜底**（finalize）：循环结束后检查待答都问出去了吗，没问就补进回复
    —— 保证**不丢**

三个概念：

    Skill（专业技能 · 静态）──┐
                              ├─► Task（一次具体业务工作）──► Sub Agent（可选执行者 · 用完即弃）
    用户原话 ─────────────────┘

**Task 只在主 agent 决定 delegate 时才建。** 不委派就没有 Task。

## 设计原则（不变量）

1. **平台只提供事实，不替主 agent 做决定** —— "用户是不是在回答""要不要重跑"都是理解问题，
   该柔性；平台硬保证的只有**不丢**（`finalize` 无条件透传待答）。
2. **`Task` 存在 Graph State 里**（**不是独立的业务实体**）——它的生命周期就是图执行的一部分，
   不跨 thread、不需要独立查询。**什么时候该独立出去**（`TaskStage` 暂缺的
   `pending` / `waiting_for_dependency` / `cancelled` 三个状态各要什么条件才加）
   见 `proto/handoff/ledger.py` 模块头的「遗留问题」。
3. **`log` 是渲染产物，不许拿它做判据** —— 断言 / 报告读 `actions`（结构化事实）〔沿革 10〕。
4. **子图 schema 必须窄** —— 子图会把它声明过的字段**原样回吐**给父图，而 `log` / `actions`
   的 reducer 是 `operator.add`：每轮翻一倍〔沿革 2〕。带 `operator.add` 的字段不许进子图。
5. **本轮信号本轮写** —— `answer_to` / `fed_asking` 只在本轮有效，`node_feed` **只有一条返回
   路径**（没有第二条可漏）〔沿革 29〕〔沿革 31〕。
6. **定向投递，平台不猜** —— "这句在答谁"由**通道**标（CLI 的答复提示符 / case YAML），
   平台读标识、不读文本〔沿革 23〕。
7. **"取哪句 / 取哪条"各只有一处定义**（`asked_text` / `next_pending`）—— 别在 CLI / 跑批里
   各写一套。

## 关键取舍

**① 投递与台账拆成两个节点**（`feed → prepare`）：投递里**同步跑执行器**（60–120 秒），
节点名原来叫 `prepare`，把最重的那件事藏起来了〔沿革 29〕。

**② 待答"一次只摆一个"**（`next_pending`）：CLI 摆哪条、用户答的就是哪条 —— **通道决定归属**；
`skill` 参数只给跑批点名用〔沿革 27〕。

**③ 渲染走唯一那条路**：`render_relay` 只管"每条待答怎么排"，段名 / 缩进 / 顺序全交给
`handoff.render_hop`（词汇表只有一份）〔沿革 24〕。

**④ 跑主 agent 用 `stream` 而不是 `invoke`**（`node_agent`）：`invoke` 会把整个循环跑完才返回，
"模型→工具→模型→工具…"全程静默〔沿革 4〕。

**⑤ 执行器走属性访问**（`executor.run_task`）而不是 from-import —— 晚绑定，`harness.install()`
的桩才生效〔沿革 18〕。

**⑥ `finalize` 那条注入留着**：撤掉它治不了复述，真正治它的是 `agent._StopWhenPending`〔沿革 21〕。

## 遗留问题

- **台账目前是全量注入**（用户确认）。若台账变长到撑上下文，可改成只注入活跃的
  （进行中 / 待答 / 最近完成的），老归档不注入。
- **接 Web 宿主时：显示走 `trace` 事件订阅，别按 `values` 流推 state**（第三轮外审 P3-3，
  用户定：*"先记住这个问题 暂不处理"*）。`node_feed` 里**同步**跑 60–120 秒的执行器
  （`dispatch` → `run_task`），而 `stream_mode="values"` 是**每个 super-step 吐一次** ——
  那 60–120 秒里它**一个 chunk 都不吐**。
  ⚠️ **只影响那一个会话**：LangGraph 把**非 async 的节点函数**包了 `run_in_executor`
  （`.venv/.../langgraph/pregel/_call.py`），所以它跑在**线程**里、不占事件循环；
  CLI 更不受影响 —— N2 之后展示层订阅事件，而事件在 `node_feed` **内部**就发出来了。
  ⇒ **它是"接宿主时的选型提醒"，不是性能缺陷。** 真要按 `values` 流推，就得把投递改成
  **异步 task** —— 图要重新长出条件边（现在是直线五节点）。
  ⚠️ 真会**跨会话**的是**线程池饱和**（uvicorn 默认 40 线程）：40 个会话各占一个线程
  120 秒，第 41 个请求开始排队 —— 那是**容量规划**，不是这段代码的缺陷。
  🔴 **⚠️ 上面那句"只影响一个会话"只在"节点跑在线程里"时成立** —— 而那靠的是
  LangGraph 的 `run_in_executor`。**迁到纯 asyncio（节点改 `async def`）之后没有线程池兜底**：
  那时一个**同步阻塞调用卡住的是整个事件循环**，不只一个会话。
  ⇒ **迁移时 `node_feed` 这条链必须真 `await` 掉**（不是可选）。反过来：真 await 之后
  60–120s 变成协作式的，**线程池饱和也一并消失** —— 纯 asyncio 就是这条的正解。

## 沿革（改过什么、为什么改）

> 编号是**引用锚**（正文里写 `〔沿革 N〕`）—— **只追加、不改号**，新条目加在**末尾**。

1. **`_merge_tasks` 的由来**（实测踩过）。模型会**并行**发多个 `delegate` 调用，每个工具都
   读到同一份旧 state —— 直接覆盖会**只剩最后一个 Task**（三个任务的 id 全变成 `t1`，
   最终只留下一个）。所以 `tasks` 必须走 reducer。
2. **`AgentState` 为什么必须窄**。子图把它状态里的字段**原样回吐**给父图，而父图的 `log`
   reducer 是 `operator.add` —— 父图自己的 log 被子图又加了一遍，**每轮翻一倍**：
   `1 → 5 → 12 → 27 → …`。实测（`experiments/probe_double_prepare.py`）：`prepare` 明明只
   返回 3 条，`finalize` 进来却看到 8 条。⚠️ **这是既有 bug，不是新引入的**：改动前的
   `baseline` 日志就是 `[1, 5, 12]`；以前没暴露，是因为那些 run 走了"主 agent 先问用户、
   不派发"的路径，`prepare` 直接返回 `{}`，**一条 log 都不产生**。`tasks` 不受影响
   （reducer 按 id 合并，幂等）。
3. **`node_agent` 为什么要逐步骤 dump**。主 agent 的消息**不进** `run_task` 的轨迹 dump
   （那是执行器的），所以它的 `delegate` / `read_artifact` / `write_artifact` 在日志里
   原来**一点痕迹都没有**。实测踩过：一个 case 里主 agent 读了 5162 字的产物，日志里查不到，
   只能从"3 个 ToolMessage 但只有 2 次 delegate"反推 —— **日志的职责是让这种事直接可查，
   不靠推断。**
4. **`stream` 换掉 `invoke`**（2026-10-30 改）。`invoke` 会把**整个 agent 循环**跑完才返回，
   于是"模型→工具→模型→工具…"这一串**全程静默**。实测一次真实会话：主 agent 连调 8 个工具、
   **336 秒里日志一行不动** —— 而 CLI 是 tail 日志的，用户侧就是**黑屏**，分不清"在干活"
   还是"卡住了"。改成 `stream(stream_mode="values")` 后**每个 super-step 结束就 dump 一次**，
   静默窗口从"整个循环"缩到"一次 LLM 调用"。
5. **台账注入的由来**（S14）。不注入的话，主 agent 只看得到自己对话历史里那几条工具返回，
   **不知道已经派过什么** —— 实测后果就是"用户补一句，它把三个任务全部重跑"。
6. **`_which_unasked` 删掉**（2026-10-29，用户明确要求）。原话：*"能用代码用机制保证的
   一定不要用 llm 自行决策"*。那个判断**两头都会错**：判"问出去了"其实没问 → 问题静默丢失；
   判"没问出去"其实问了 → 用户看到重复。而且它还有个更糟的后果 —— 它让"问题会不会到用户
   手上"这件事**依赖 LLM**，而这是平台该保证的事。
7. **原话投递，平台不做判断**（2026-10-29，用户定）。原话：*"只要是答复了 就直接恢复
   subagent 就行啊"* · *"答非所问就答非所问呗"*。原话投过去，执行器自己判断够不够，
   不够就再问一遍。**平台不替用户判断他的话算不算回答** —— 那是语义判断，而且判断错了
   比"多问一遍"更糟（F7 就是这么丢的）。
8. **历次答复必须累积**（2026-10-29 人在环实测暴露）。执行器是隔离上下文、**用完即弃**，
   每次投递都是一次全新的 `run_task`；只带"最新一条"的话，用户一次答一个时，**答第二个
   就把第一个冲掉了**。实测现象：问"用途"→答"课前预习"→问"填写形式"→答"A"→**又问一遍
   "用途"**，用户被同一个问题问两次（`_feed_waiting` 的 R03/R04/R05 三次投递，每次都只带
   当轮）。脚本化跑批测不出来：预置答复是一句话把两问都答了。
9. **一问一答成对存**（用户 2026-11-02）。`report` 是**覆盖**写的（`absorb_outcome`），
   只存答案的话，第二次投递之后旧答案就没有指涉了 —— 用户看到的是"答：A"，却看不到 A
   在回答什么。
10. **`actions` 为什么单开一个字段**（2026-11-04）。`log` 是**渲染给人看的**，而断言原来在它
    上面做子串匹配（`"finalize: 透传" in ln`）—— 改一句文案，断言就**静默给出错误结论**
    （`check_platform_relayed` 会报"没看到透传"）。判据见 CLAUDE.md 判据一：**渲染产物不许
    被反解析回结构**。对象在**发生那一刻**由平台自己写（它手里就有事实），断言 / 报告
    **读对象**；`log` 那句人话仍在**同一个调用点**写 —— 两者同源（同一批事实），各司其职。
11. **状态迁移收进 `settle()`**（2026-11-03 审核实测踩到）。原来这里手写
    `rec.update(stage="completed", artifacts=outcome.artifacts)`，会把"这一轮没产出就不抹掉
    上一轮产物"的保护抵消。
12. **note 的前缀归记录器**（2026-11-03 审核 F1）。`finalize: ` 原来是**业务侧**写进正文的，
    展示层于是按**位置**切回来 —— 只改文案措辞，上屏就被**静默吃掉 10 个字**（实测）。
13. **透传那条 note 不再写「谁 → 谁」**。展示层上屏时已经加了前缀（`chat._on_event`：
    `"平台 → 用户：" + ev["text"]`），记录层文件里紧跟着的 `📤 交接 · 待答透传（平台 → 用户）`
    也写着 —— **再说一遍就是第三处** ✗（实测：终端上会印成
    `平台 → 用户：**平台 → 用户**：透传…`）。这条只留**这一跳的机制说明**（F7 的修法：
    平台直接贴、不判断主 agent 问没问）。
14. **注入什么就记什么**（用户 2026-11-03）。原话：*"这些在日志中得完整记录下来便于复盘"*。
    两处同一天改的：`prepare` 原来只记"注入 N 项"、**正文一个字都不在日志里**；透传那段原来
    只在 `_run.md` 里有（走快照），trace 里一个字都没有 —— 复盘时看不到"平台替执行器转达了
    什么"。
15. **透传的膨胀账**（判据③，2026-11-03 → 2026-11-05）。透传那段原来和**回执的【执行器说】**
    是**同一段话** —— 一轮之内它会进主 agent 的上下文**两次**（实测：两条 `report` 合计 936 字，
    各出现 2 次）。2026-11-05 起这个膨胀点基本消失了：透传改成贴 `Task.asked`（**那句问题**），
    不再是 `report`（那段独白）—— 用户看到的是短问题 ✓，进上下文的也短了 ✓（剩下的账：
    `report` 仍在**回执**里全文出现一次 ✓ 那是它该在的地方）。⚠️ **不能靠"去重"解决** ——
    那需要判断"主 agent 说没说"，是 LLM 判断（2026-10-29 已否决）。
    见 `docs/handoff-audit.md` §四 膨胀账。
16. **「问」改取 `Task.asked`**（2026-11-04 日志定位）。`report` 是执行器**最后说的那段话**
    （混着"好的，第一个问题已发出，等你的答复"这类过场），平台自己在 `agent._ask_main` 里
    都标着"可能只是过场" —— 拿它当「问」，配对就成了"一句没有指涉的话 ＋ 一个答复"。
    实测：**同一句问题两副面孔** —— 透传给用户的是干净
    原话（`finalize` 走 `Task.asked`），回灌给执行器的却是那段过场。而 `asked` 就在手边：
    `settle` 要到下面那一行才覆盖它。⚠️ 取不到时**不静默降级**（fixture / 手写 state）：
    存空串，由 `briefing` 明说"这一条没留下问题原话"（同 `finalize._one` 的写法）——
    **绝不拿 `report` 顶替**，那正是这次要修的错。
17. **`trigger="feed"` 的由来**（2026-11-04 复审 E2）。`trigger="feed"` = **这轮是平台起的**
    （不是主 agent 派发的）：台账据此说清"全文在哪" —— 这一趟**没有回执**，全文不在主 agent
    上下文里。不记这个事实的话，台账会指着一个**存在但内容不对**的出处。
18. **执行器必须走属性访问**（2026-11-04 复审 N3 修）。`_feed_waiting` 里原来是**函数内**
    `from proto.executor import run_task` —— 那**不是**为了破环（executor 从不 import graph），
    而是**晚绑定**：`harness.install()` 的记录器靠改 `proto.executor.run_task` 打桩，函数形式
    的 import 会在 import 那一刻把函数**绑死**。⚠️ **实测踩过**：把函数内 import 直接提到
    顶层 → `unit_feed_waiting` 打到真 LLM、卡住 180 秒。所以这里**必须**走属性访问。
19. **`fed_asking` 两个 schema 都要声明**（2026-11-04 实测踩到）。少一个就**静默失效**：
    `State` 没声明的话 LangGraph 会把那个键**悄悄丢掉**（不报错、不警告）；`AgentState` 少了，
    子图里的 `_StopWhenPending` 读不到。只加了一边的那一跑：拦下的 2 次**全是「本批回执」
    那条**，这一条一次没生效。守卫见 `unit_stop_on_pending` ⑦。
20. **产物路径那条入口**（2026-11-04 日志定位）。`feed` 那条路**没有回执**（`Task.run_by`），
    所以投递重跑之后主 agent 拿到路径的**唯一入口**是 `list_artifacts` —— 那个入口当时
    **不给路径**，于是它报给用户的是**自己编的** `art_001.md`（真实文件叫
    `art_001_荷花-教案.md`）。现在 `artifact_index_lines` 带路径（那一层是按需的，台账照旧
    只放 ID）。
21. **注入留着（F18 收口）**（2026-11-04）。曾经试过**撤掉** `finalize` 那条注入（理由：模型
    在自己的上下文里看到 `AIMessage: 【lesson-prep】问：…`，**以为是自己说过的话**，于是在
    回复里又写一遍）。⚠️ **实测无效**：撤掉之后主 agent **照样复述**（4/4），因为问题内容还从
    **台账**和**回执**进来 —— 只要它知道，它就会写。**真正治它的是 `agent._StopWhenPending`**：
    执行器一申报待答，主 agent **那一拍根本不生成**。**问题出在"它有机会说"，不在"它看得见"。**
    ⇒ 于是这条注入**该留着**：它是**转写里唯一那条问题**（CLI 上由 `next_pending` 的提示符
    呈现），而且 `_user_turns` 的「它在回答的上一句」正是靠它才有内容（撤掉注入时实测：
    那一行会退化成主 agent 的旧回复）。
22. **谁答复了谁的什么问题**（用户 2026-11-05）。答复的是**用户**、回答的是**执行器问的那句**
    （`Task.asked`，不是 `report` 那段独白）。平台只是**转达**，所以在 `structured` 里点明它
    —— 不能因为"经手的是平台"就把"谁说的"吞掉。
23. **定向投递**（2026-11-05，`solution.md` §0.65 Y1 第二步）。原来这里是**广播**（投给所有
    待答）：一句话只答了一个，其余全都要白跑一趟（各 60–120 秒，实测 3.8% 的轮次会碰上）。
    **平台不猜"这句在答谁"** —— 那是语义判断（F7 的死因）；标识来自**通道**，不是读文本。
24. **【待答】段名与唯一渲染路**（2026-11-05）。在这之前每条写成 `【{skill}】问：…` ——
    **拿段名的括号包技能名** ✗（`【…】` 在这套系统里是**段名的专用符号**，读的人会以为那是
    段名；记录层切段也靠它）。现在技能名用 `「」`（和台账一致），渲染走 `render_hop`。
25. **`render_relay` 抽成模块级纯函数**。它原来嵌在 `node_finalize` 里，渲染结果就只剩
    `trace.handoff` 那条记录，断言够不着了（同 `ask.build_prompt` / `nudge.nudge_text`）。
26. **兜底那条必须说话**（2026-11-05 修）。`invoke` 会把整个 agent 循环**跑完**才返回 ——
    也就是**这一趟全程没有流式日志**，正是 `node_agent` 存在的全部理由要治的黑屏（实测一次
    真实会话：连调 8 个工具、**336 秒日志一行不动**）。原来这里一个字都不写 ✗ —— 真走到
    这条路上，读日志的人只会以为"卡住了"。
27. **待答"一次只摆一个"**（用户 2026-11-05 定）。不指定技能时摆的是**第一个**待答
    （`tasks` 按派发顺序累积）；**CLI 摆哪条，用户答的就是哪条**。`skill` 参数是给**跑批的
    case** 用的：多个技能并行派发时"第一个"取决于谁先回来（不确定 ✗），脚本写的答复得能点名。
28. **编排收进 `dispatch` 一处**（外审 F5 的收口，2026-11-06）。这五步（run_task → 先落账 →
    `run_by` → 试答重跑 → `settle` → 取代哑待答）原来这条路上各写一遍、`delegate` 那条也
    各写一遍 —— 于是顺序漂了（实测：这条路的试答重跑拿到的是**上一轮**的 report）。
    顺序写两遍就一定会漂。
29. **投递与台账拆两个节点**（2026-11-06，外审 F4）。原来挤在一个 `prepare` 里，
    "先投递、后注入台账"（台账才反映投递后的新状态）只能靠注释提醒 —— 现在**顺序就是图的
    边**，改不动也藏不住。同一次把 `answer_to` / `fed_asking` 这两个本轮信号收进 `feed`：
    它只有一条返回路径，**没有第二条可漏**（守卫 `unit_feed_waiting` ⑫ 也跟着改成断**条数**）。
30. **哑待答显式取代**（2026-11-06）。**这条完成了 → 同技能更早的待答成了哑问题** ——
    判据与边界见 `moot_older_waiting`（快照没延长就一个字不碰 —— F7 的场景不动）。
31. **`answer_to` 的"用后即清"曾经只是声称**（2026-11-06 外审 N1 实测）。拆之前主返回路径
    漏写过它（早返回写了）—— 于是"在 `↩` 下答过一次"之后的**每一轮**都带着残留的 `answer_to`：
    用户回车跳过待答、回 `👤` 说的**任何新消息**都会被 `_feed_waiting` 当成"在回答那条"投给
    旧任务（每次白跑一趟执行器 ＋ 答非所问，**持续**如此）。实测（真 checkpointer）：
    三轮都误投。现在由 `unit_feed_waiting` ⑫ 按 AST 盯着**每一条**
    `return`（"每条返回路径都要写"这类规矩**不能只写在注释里**）。
32. **删掉一条死 import ＋ 两句跟着它过时的注释**（2026-11-06，Pylance 翻出来的）。
    `from proto import executor` 是〔沿革 28〕把调用收进 `dispatch` 之后**留下的**：
    执行器调用搬去了 `agent.py`，而**这个 import 和它的说明没跟着走**。于是两处注释都在
    讲一件**这里已经不做的事**——"下面这行**必须**走属性访问"（下面那行根本没被用）、
    "**属性访问**（`executor.run_task`）而不是 from-import"（这里没有属性访问）。
    全文件 `grep executor` **只剩注释命中**。⇒ 删 import，两处注释改成**指向真正调它的地方**
    （`agent.dispatch`）；`harness.install()` 那句"两个文件都走模块属性"也一起改准。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, NotRequired, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from proto import trace
from proto.agent import build_agent
from proto.agent import dispatch  # 派发编排（两条路共用，F5 收口）
from proto.handoff.briefing import asked_text
from proto.handoff import render_hop  # 唯一那条渲染路（见 handoff/__init__）
from proto.handoff.ledger import render_ledger
# ⚠️ **本模块不直接调执行器**（原来有 `from proto import executor`，是**死 import**）：
#    投递那条路走 `agent.dispatch`，执行器调用在**那边**，走的是
#    `executor.run_task` 的**属性访问**（晚绑定 —— `harness.install()` 的记录器靠改
#    `proto.executor.run_task` 打桩，`from … import run_task` 会在 import 那一刻把函数
#    **绑死**，桩就不生效了〔沿革 18〕）。〔沿革 32〕
from proto.registry import registry  # 技能注册表（唯一持有者）


def _merge_tasks(old: list[dict] | None, new: list[dict] | None) -> list[dict]:
    """按 id 合并 Task —— `tasks` 的 reducer。

    ⚠️ **必须用 reducer**：模型会**并行**发多个 `delegate` 调用，每个工具都读到同一份
    旧 state —— 直接覆盖会**只剩最后一个 Task**〔沿革 1〕。
    """
    merged: dict[str, dict] = {t["id"]: t for t in (old or [])}
    for t in new or []:
        merged[t["id"]] = t
    return list(merged.values())


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    #: 只记录"主 agent 决定委派出去的工作"。不委派就是空 —— 这是 Task 概念在新设计里的落点
    tasks: NotRequired[Annotated[list[dict[str, Any]], _merge_tasks]]
    #: 平台这一轮干了什么 —— **给人看的渲染记录**（`_run.md`、CLI 的 delta）。
    #: ⚠️ **不许拿它做判据**（断言 / 报告都不许）—— 它是渲染产物，改一句文案就静默失配〔沿革 10〕。
    log: NotRequired[Annotated[list[str], operator.add]]
    #: **平台动作** —— 结构化的事实（谁做的 / 做了什么 / 涉及哪些 Task / 多少字）。
    #:
    #: 🔴 **为什么要单开一个字段**：断言 / 报告**读对象**，不读 `log` 那句人话 ——
    #: 两者在**同一个调用点**写（同一批事实，各司其职）〔沿革 10〕。
    #: ⚠️ **别加进 `AgentState`**：子图会把声明过的字段原样回吐给父图，而这里的 reducer
    #: 是 `operator.add` —— 每轮翻一倍〔沿革 2〕。
    actions: NotRequired[Annotated[list[dict[str, Any]], operator.add]]
    #: 🔴 **这一条用户消息是在回答哪个待答任务**（`solution.md` §0.65 Y1 第二步）。
    #:
    #: **由通道填，平台不猜**：CLI 的答复提示符（`↩ `）/ case YAML / 跑批子进程。
    #: - 有它 → `_feed_waiting` **只投给那一个**任务；
    #: - 没有 → **一条都不投**（那是主 agent 的输入）。
    #:
    #: ⚠️ **用后即清**（`node_feed` 写回 `""` —— 它**只有一条返回路径**，没有第二条可漏）：
    #: 不清的话它跟着 checkpointer 活到下一轮，**后面每一轮都会被当成"在回答那条"** ✗〔沿革 31〕。
    #: ⚠️ 也**别加进 `AgentState`**：理由同 `actions`。
    answer_to: NotRequired[str]
    #: 🔴 **这一轮投递之后，执行器又问了一个**（`discussion-log` F18 的
    #: **「投递后又问」那一形状** —— 见 `agent._StopWhenPending`）。
    #:
    #: ⚠️ **两个 schema 都要声明，少一个就静默失效**〔沿革 19〕：
    #:   · `State`（这里）—— `node_feed` 要往**父图**写它；
    #:     **没声明的话 LangGraph 会把那个键悄悄丢掉**（不报错、不警告）；
    #:   · `AgentState` —— 子图里的 `agent._StopWhenPending` 要**读**它。
    #:
    #: ⚠️ 它是 **bool、没有 reducer**，所以子图回吐同一个值 —— 不会像 `log` 那样翻倍
    #: （`AgentState` 那条"别加进来"的警告针对的是带 `operator.add` 的字段）。
    fed_asking: NotRequired[bool]


class AgentState(TypedDict):
    """主 agent **子图**的状态 —— **故意不含 `log`**。

    🔴 **为什么必须窄**：子图会把它状态里的字段**原样回吐**给父图，而父图的 `log` reducer
    是 `operator.add` —— 父图自己的 log 被子图又加了一遍，**每轮翻一倍**（指数）〔沿革 2〕。

    `tasks` 不受影响 —— 它的 reducer 按 id 合并，是幂等的。
    """

    messages: Annotated[list[AnyMessage], add_messages]
    tasks: NotRequired[Annotated[list[dict[str, Any]], _merge_tasks]]
    #: 🔴 **这一轮投递之后，执行器又问了一个**（`discussion-log` F18 的
    #: **「投递后又问」那一形状**）。
    #:
    #: **为什么子图需要它**：`agent._StopWhenPending`（主 agent 的 `before_model` 中间件）
    #: 要判断"这一轮平台会不会贴一条新问题给用户"。待答只可能在两处产生：
    #:   · **「本批回执」**（主 agent 自己调 `delegate` → 回执 waiting）—— 它扫**工具回执**就能认；
    #:   · **「投递后又问」**（平台在 `feed` 里把用户答复投给执行器 → 执行器又问了一个）——
    #:     这一轮主 agent 手里**什么都没有**（消息末尾是 `HumanMessage`），
    #:     所以只能由平台**把事实写在这儿**。
    #:
    #: ⚠️ **本轮有效**（`node_feed` 每轮都写 True/False —— 它只有一条返回路径）——
    #: 它不是"长期挂着的 pending"。漏写它就会残留到下一轮，
    #: 于是"上一轮执行器提问、下一轮用户问天气"时主 agent 被**无故收话**（同 `answer_to`）。
    #: ⚠️ 它是 **bool、没有 reducer** —— 子图回吐同一个值，不会像 `log` 那样每轮翻倍。
    fed_asking: NotRequired[bool]


# ────────────────── feed：投递用户原话（会跑执行器）／prepare：注入台账 ──────────────────


def _last_human(state: State) -> str:
    for m in reversed(state.get("messages") or []):
        if isinstance(m, HumanMessage) and str(m.content).strip():
            return str(m.content)
    return ""



async def _feed_waiting(state: State) -> tuple[list[dict], list[str]]:
    """把用户这条消息**原话**投给**它标明的那一个**待答任务（`answer_to`）。

    🔴 **不做任何判断** —— 不判断"用户是不是在回答""答的是哪一条"。原话投过去，执行器
    自己判断够不够；不够就再问一遍。**平台不替用户判断他的话算不算回答** —— 那是语义
    判断，而且判断错了比"多问一遍"更糟（F7 就是这么丢的）〔沿革 7〕。

    这是**答复 / 投递**这条路上唯一的解除路径：投递后要么 completed，要么还在等。
    ⚠️ 但**不是「待答唯一的解除路径」** —— 另有一条：新任务完成且用户已另起话题时，
    `moot_older_waiting` 会把旧待答标成 `superseded`（`briefing.py`，〔沿革 30〕）。

    🔴 **定向投递，不是广播**（`solution.md` §0.65 Y1 第二步）：只投给**这条消息标明的
    那个任务**（`answer_to` —— **通道**填的：CLI 的答复提示符 / case YAML）；
    **没有标识 = 主 agent 的输入，一条都不投**〔沿革 23〕。
    """
    user = _last_human(state)
    all_waiting = [
        t for t in (state.get("tasks") or []) if t.get("stage") == "waiting_for_input"
    ]
    # 🔴 **定向投递**：只投 `answer_to` 指的那一条（标识由**通道**填，平台不猜）——
    #    没有标识 = 主 agent 的输入，一条都不投（见 docstring）〔沿革 23〕。
    target = str(state.get("answer_to") or "").strip()
    waiting = [t for t in all_waiting if t.get("id") == target] if target else []
    if not user or not waiting:
        if target and not waiting:
            trace.note(
                f"⚠️ `answer_to={target}` 指的那条**不在待答**（已被取代 / 已完成 / 不存在）"
                f" → 不投递（当前待答 {len(all_waiting)} 条）"
            )
        return [], []

    out: list[dict] = []
    logs = [f"feed: 定向投递 → {target}（`answer_to` 指的那条；其余待答不动）"]
    for t in waiting:
        skill = registry.get(t["skill"])
        if skill is None:
            logs.append(f"feed: ⚠️ {t['id']} 的技能「{t['skill']}」不存在，跳过")
            continue

        # 🔴 **历次答复必须累积**：执行器是隔离上下文、**用完即弃**，每次投递都是一次全新的
        #    `run_task` —— 只带"最新一条"的话，用户一次答一个时**答第二个就把第一个冲掉了**〔沿革 8〕。
        # 🔴 **一问一答成对存**：`report` 是**覆盖**写的（`absorb_outcome`），只存答案的话，
        #    第二次投递之后旧答案就没有指涉了（用户看到"答：A"，看不到 A 在回答什么）〔沿革 9〕。
        # 🔴 **「问」取 `Task.asked`，不取 `report`**：`report` 是它最后说的那段话（混着过场），
        #    拿它当「问」配对就成了"一句没有指涉的话 ＋ 一个答复"〔沿革 16〕。
        # ⚠️ 取不到时**不静默降级**（fixture / 手写 state）：存空串，由 `briefing` 明说
        #    "这一条没留下问题原话"（同 `finalize._one` 的写法）—— **绝不拿 `report` 顶替**。
        # 答复**累积在 Task 上**，整份交接书一起喂 —— 见 `agent.render_briefing`。
        rec = {**t, "qa": [*(t.get("qa") or []),
                           {"by": "用户", "q": t.get("asked") or "", "a": user}]}
        trace.section(f"feed · 投递用户原话 → {t['id']} · {t['skill']}", kind="feed")
        trace.block("投过去的原话", user)
        trace.note(f"累计问答 {len(rec['qa'])} 对（历次一起带上，否则执行器会重问）")
        # 交接关键日志：投递这一跳（交接书正文由 `run_task` 那边记）
        # 🔴 **谁答复了谁的什么问题**：答复的是**用户**、回答的是**执行器问的那句** ——
        #    平台只是**转达**，别把"谁说的"吞掉（那正是这一行原来读不出来的东西）〔沿革 22〕。
        # ⚠️ 「问哪句」走 `asked_text`（**取哪句只有一处定义**，见它的 docstring）。
        trace.handoff("答复投递", "用户", "执行器", user,
                      structured=f"平台转达｜在回答它问的那句：{asked_text(t)}"
                                 f"｜Task.qa 追加 1 对（累计 {len(rec['qa'])} 对）")
        # ⚠️ `refs` 要一起带上：不重建可读产物清单，执行器在续跑时就"看不见"前序产物了。
        # 用户原话也一起带上（快照在 Task 上，所以这条重入路径**自动**有它）。
        # ⚠️ 这里**不**直接调执行器 —— `dispatch` 内部走 `executor.run_task` 的**属性访问**
        #    （晚绑定，桩才生效〔沿革 18〕）。
        # 🔴 **编排走两条路共用的那个函数**（`dispatch`）：run_task → **先落账**（重入前要
        #    `render_briefing`，见 `absorb_outcome`）→ `run_by` → 试答重跑 → `settle` →
        #    取代哑待答 —— 顺序写两遍就一定会漂，收进一处〔沿革 28〕。
        #    ⚠️ 试答成对的「问」在上面就取好了（`t.get("asked")`，**旧**那句 = 用户正在答的
        #    那句 ✓）—— `dispatch` 里的落账不碰它（`rec` 与 `t` 是两份）。
        #    ⚠️ `trigger="feed"` = **这轮是平台起的**（不是主 agent 派发的）：台账据此说清
        #    "全文在哪" —— 这一趟**没有回执**，全文不在主 agent 上下文里〔沿革 17〕。
        outcome, answer, mooted = await dispatch(
            skill, rec, state.get("tasks") or [],
            trigger="feed", messages=state.get("messages") or [],
        )
        if answer:
            logs.append(
                f"feed: 🔁 {t['id']} 仍缺信息 → 主 agent 试答（{len(answer)} 字）后重跑"
            )
        # ⚠️ 状态迁移走 `settle()`（**唯一一处**，在 `dispatch` 里）—— 三条分支只决定"日志怎么说"
        #    〔沿革 11〕。
        if outcome.status == "completed":
            # 🔴 **这条完成了 → 同技能更早的待答成了哑问题** —— `dispatch` 里算好带出来的
            #    （判据与边界见 `moot_older_waiting`：快照没延长就一个字不碰 —— F7 的场景不动）〔沿革 30〕。
            out.extend(mooted)
            logs.append(
                f"feed: ✅ {t['id']}（{t['skill']}）答复后**完成**"
                f"　产物={outcome.artifacts or '（在回复里）'}"
            )
        elif outcome.status == "waiting_for_input":
            logs.append(f"feed: ⏳ {t['id']}（{t['skill']}）答复后**还是缺信息** → 仍待答")
        else:
            logs.append(f"feed: ❌ {t['id']}（{t['skill']}）答复后仍未完成 → failed")
        out.append(rec)
    return out, logs


async def node_feed(state: State) -> dict:
    """① 把用户原话投给**它标明的那一个**待答任务（见 `_feed_waiting`）。

    🔴 **为什么单起一个节点**：这一步里**同步跑执行器**（60–120 秒），而原来的节点名
    `prepare` 只说了"准备 / 注入台账" —— 名字把最重的那件事藏起来了。拆开之后图上是
    `feed → prepare`：**顺序由图的边保证**（先投递、后注入台账 —— 台账才反映投递后的
    新状态），不用再靠注释提醒〔沿革 29〕。

    🔴 **只有一条返回路径**（这是有意的）：`answer_to` / `fed_asking` 都是**本轮信号**，
    漏写一条返回路径它们就会残留到下一轮。**一条路 = 没有第二条可漏**〔沿革 31〕。
    守卫见 `unit_feed_waiting` ⑫（AST 盯 `return` 的键）。
    """
    updated, logs = await _feed_waiting(state)

    # 🔴 **这一轮投递之后，执行器又问了一个吗** —— 判据是**结构事实**：`settle()` 把投递过的
    #    那条落成了 `waiting_for_input`（投递后完成了就是 `completed`，不会误判）。
    #    ⚠️ **本轮信号，本轮写**（它是 bool、没有 reducer）—— 漏写就会残留到下一轮
    #    （同 `answer_to`，见 `State.fed_asking`）。
    fed_asking = any(t.get("stage") == "waiting_for_input" for t in updated)

    fed = [t["id"] for t in updated
           if t.get("run_by") == "feed" and t.get("stage") != "superseded"]
    actions = (
        [{"by": "platform.feed", "did": "feed", "tasks": fed,
          "chars": len(_last_human(state))}]
        if fed else []
    )
    return {
        "tasks": updated,           # reducer 按 id 合并
        "log": logs,                # `feed: …`（投递那几行）—— reducer 是 operator.add
        "actions": actions,
        # ⚠️ `answer_to` / `fed_asking` 都是**本轮信号**，**必须**在这唯一一条返回路径上
        #    写回（拆之前主返回路径漏写过 `answer_to`，后果见〔沿革 31〕）。
        "answer_to": "",
        "fed_asking": fed_asking,
    }


def node_prepare(state: State) -> dict:
    """② 把**任务台账**注入主 agent 上下文。

    **投递已经在 `feed` 里做完了**（顺序由图的边保证：`feed → prepare`）—— 所以这里读到的
    `state["tasks"]` **已经是投递后的最新状态**（reducer 在 `feed` 返回那一刻就合过了，
    不用再自己合一遍）。

    **只提供事实，不替它做决定**（"要不要重跑"是理解问题，该柔性）。

    为什么要注入：不注入的话，主 agent 只看得到自己对话历史里那几条工具返回，
    **不知道已经派过什么**〔沿革 5〕。

    ⚠️ **目前是全量注入**（用户确认）—— 欠账见模块头「遗留问题」。
    """
    tasks = state.get("tasks") or []
    if not tasks:
        # 闲聊轮次：没有台账可注入，一个字都不加。
        # ⚠️ 这里**只剩这一件事** —— `answer_to` / `fed_asking` 那种"每条返回路径都要写"
        #    的本轮信号已经跟着投递搬到 `feed` 了（那个节点只有一条返回路径）。
        return {}

    # 台账怎么渲染在 `proto/handoff/ledger.py`（schema 与模板放一起）
    waiting = [t for t in tasks if t.get("stage") == "waiting_for_input"]
    text = render_ledger(tasks)
    # 🔴 **注入什么就记什么**：这一行是平台每轮塞给主 agent 的东西，复盘时最该看到的就是它
    #    （原来只记"注入 N 项"，正文一个字都不在日志里）〔沿革 14〕。见 `trace.handoff`。
    trace.handoff("台账", "平台", "主 agent", text,
                  structured=f"Task × {len(tasks)} → render_ledger", full=True)
    return {
        "messages": [SystemMessage(content=text)],
        "log": [f"prepare: 注入台账（{len(tasks)} 项，其中待答 {len(waiting)} 条）"],
    }


# ──────────────────────────── finalize：兜底补问 ────────────────────────────


def render_relay(waiting: list[dict]) -> str:
    """待答 → **贴给用户的那段文本**（`solution.md` §0.65 Y1 的第一步）。

    🔴 **从对象渲染**：每条待答贴的是它**申报的那句原话**（`Task.asked` —— `ask_user`
    的参数，`absorb_outcome` 原样落下），**不是** `report`（执行器最后说的**那段话**，
    混着"好的，我先来进行第一步的分析…"之类的过场）。**用户要答的是"问题"，不是那段独白。**

    ⚠️ `asked` 缺失时（`case.initial_tasks` 注入的 fixture、手写的 state）**不许静默降级**：
    显式说清"这条没留下问题原话"，再把那段话附上 —— 有就说有，没有就说没有。

    ⚠️ **抽成模块级纯函数是为了可单测**（同 `ask.build_prompt` / `nudge.nudge_text`）：
    它原来嵌在 `node_finalize` 里，渲染结果就只剩 `trace.handoff` 那条记录，
    断言够不着了〔沿革 25〕。

    ⚠️ **走唯一那条渲染路**（`render_hop`），段名是【待答】；技能名用 `「」` ——
    **不许拿段名的括号包技能名**（`【…】` 是段名专用符号）〔沿革 24〕。
    """
    def _one(t: dict) -> str:
        who = t.get("skill") or "?"
        # ⚠️ **"取哪句、取不到怎么办"只有一处定义**（`asked_text`）——
        #    CLI 的提示符（另一个**视图**）也用它。见那个函数的 docstring。
        text = asked_text(t)
        if "\n" not in text:
            return f"- 「{who}」问：{text}"
        # ⚠️ **续行自己在里面缩 2 格**（`render_hop` 会把整段再缩 2 格 → 共 4 格）——
        #    不然多行的那段话会顶到和 `- ` 同一列，看起来像另一个列表项。
        return f"- 「{who}」问：\n" + "\n".join(f"  {ln}" for ln in text.splitlines())

    return render_hop("relay", {"relay": "\n".join(_one(t) for t in waiting)})


def node_finalize(state: State) -> dict:
    """**透传**：待答原样贴进回复，保证问题一定到用户手上。

    🔴 **这里原来是 LLM 判断**（`_which_unasked`：问"主 agent 问出去了吗"，没问出去才补）。
    已删除 —— 那个判断**两头都会错**（判"问出去了"其实没问 → 问题静默丢失；判"没问出去"
    其实问了 → 用户看到重复），而且它让"问题会不会到用户手上"这件事**依赖 LLM**〔沿革 6〕。

    ⚠️ **"原样"贴的是它申报的那句原话**（`Task.asked`）—— 不是执行器最后说的那段话
    （那段混着过场，见 `render_relay` 的注）。

    **现在的分工**：
    - **保证**（代码）：有待答就原样贴出去，无条件
    - **礼貌**（提示词）：`prepare` 会告诉主 agent "平台负责转达，你不用重复问"
      —— 它照做了就没有重复；没照做也只是重复一句，**不影响不丢**

    ⚠️ **产物路径不在这里贴** —— 那条信息顺着 `delegate` 的返回走（见 `agent.render_receipt`
    的【产出的产物】）：路径是 `write_artifact` 的返回里就有的，平台**顺着原路带上去**就够了，
    不需要另起一套注入。
    🔴 **但 `feed` 那条路没有回执**（`Task.run_by`）—— 所以投递重跑之后，主 agent 拿到路径的
    **唯一入口**是 `list_artifacts`，它现在带路径（那一层是按需的，台账照旧只放 ID）〔沿革 20〕。
    """
    waiting = [t for t in (state.get("tasks") or []) if t.get("stage") == "waiting_for_input"]
    if not waiting:
        return {}

    block = render_relay(waiting)
    # ⚠️ **只发内容**：`finalize: ` 那个前缀是**日志文件格式**的一部分，归记录器
    #    （`trace._NOTE_HEAD`）〔沿革 12〕。
    # ⚠️ **这行不写「谁 → 谁」** —— 展示层上屏和记录层文件里都已经写着，**再说一遍就是
    #    第三处** ✗（实测：终端上会印成 `平台 → 用户：**平台 → 用户**：透传…`）〔沿革 13〕。
    trace.note(f"透传 {len(waiting)} 条待答（平台直接贴，不判断主 agent 问没问）",
               kind="relay")
    # 🔴 **（历史）已知膨胀点（判据③）**：透传这段原来和**回执的【执行器说】**是同一段话，
    #    一轮之内会进主 agent 的上下文**两次**；贴 `Task.asked` 之后基本消失〔沿革 15〕。
    # 🔴 **注入什么就记什么** —— 这段原来只在 `_run.md` 里有（走快照），trace 里一个字都没有，
    #    复盘时看不到"平台替执行器转达了什么"〔沿革 14〕。
    trace.handoff("待答透传", "平台", "用户", block,
                  structured=f"Task.asked × {len(waiting)} → AIMessage(injected_by=platform.finalize)",
                  full=True)
    return {
        # 🔴 **打标记**：这条 AIMessage 的内容是**执行器的原话**，不是主 agent 说的 ——
        #    不打标记的话，记录层会把它标成"🤖 主 agent 输出"，**来源标注在这一跳就断了**。
        #    标记挂在 `additional_kwargs` 上（实测不会漏给模型），记录层读它 —— 而不是
        #    "看内容像不像执行器的话"（那是反结构化，见 `proto/handoff` 的 docstring）〔沿革 9〕。
        #
        # 🔴 **注入留着**：撤掉它治不了复述（实测 4/4 照样复述），真正治它的是
        #    `agent._StopWhenPending` —— **问题出在"它有机会说"，不在"它看得见"**〔沿革 21〕。
        "messages": [AIMessage(content=block, additional_kwargs={"injected_by": "platform.finalize"})],
        "log": [f"finalize: 透传 {len(waiting)} 条待答（原样贴执行器的话，无条件）"],
        # 🔴 **同一刻记下结构化事实**（断言读它，不读上面那句人话）—— 见 `State.actions`
        "actions": [{
            "by": "platform.finalize", "did": "relay",
            "tasks": [t["id"] for t in waiting], "chars": len(block),
        }],
    }


# ──────────────────────────── 待答：一次摆一个 ────────────────────────────


def next_pending(state: State, skill: str = "") -> dict | None:
    """**当前该摆到用户面前的那一个待答**（`solution.md` §0.65 Y1）—— CLI 据此换提示符。

    返回那条 Task（`None` = 没有待答）。`skill` 非空时只看那个技能的。

    🔴 **"一次只摆一个"** —— 不指定技能时摆的是**第一个**待答（`tasks` 按派发顺序累积）。
    **CLI 摆哪条，用户答的就是哪条**：把它当 `answer_to` 原样带回去
    （`graph.invoke(..., {"answer_to": t["id"]})`）〔沿革 27〕。

    ⚠️ `skill` 是给**跑批的 case** 用的：多个技能**并行**派发时，"第一个"取决于谁先回来
    （不确定 ✗），而脚本写的答复是**针对某个技能**的 —— 所以 case 要能点名
    （`answer_to: "@waiting:guided-learning"`）。**CLI 不点名**（用户看到哪条答哪条）。
    ⚠️ **"取哪条"只许有这一处** —— 别在 CLI / 跑批里各写一套（"一套，别分叉"）。
    ⚠️ 它**不判断**"用户这句是不是在回答它"（那是语义判断）—— 用户在**这个提示符下**
    打的字，就属于它：**通道决定归属** ✓。
    """
    for t in state.get("tasks") or []:
        if t.get("stage") == "waiting_for_input" and (not skill or t.get("skill") == skill):
            return t
    return None


# ──────────────────────────── 装配 ────────────────────────────


def build():
    g = StateGraph(State)
    # 🔴 **两个节点，顺序由边保证**：`feed`（投递 —— 里面同步跑执行器，60–120 秒）→
    #    `prepare`（注入台账）—— 拆开后"先投递、后注入"不用再靠注释提醒〔沿革 29〕。
    g.add_node("feed", node_feed)
    g.add_node("prepare", node_prepare)
    main_agent = build_agent(AgentState)  # 主 agent：完整的 agent（工具 + 循环）

    async def node_agent(state: State, config: RunnableConfig) -> dict:
        """跑主 agent 子图，**每走一步就把它露出来**。

        🔴 **为什么必须在这里记**：主 agent 的消息**不进** `run_task` 的轨迹 dump（那是
        执行器的），它的 `delegate` / `read_artifact` / `write_artifact` 在日志里原来
        **一点痕迹都没有** —— **日志的职责是让这种事直接可查，不靠推断**〔沿革 3〕。

        🔴 **为什么是 `stream` 而不是 `invoke`**：`invoke` 会把**整个 agent 循环**跑完才
        返回，"模型→工具→模型→工具…"这一串**全程静默**；改成 `stream(stream_mode="values")`
        后**每个 super-step 结束就 dump 一次**（模型那步 → 它的工具调用；工具那步 →
        返回了什么），静默窗口从"整个循环"缩到"一次 LLM 调用"〔沿革 4〕。

        ⚠️ **这不是加日志，是把已经发生的事更早露出来** —— 行为一点没变，
        返回的仍是最终 state（`stream` 的最后一个 chunk）。
        """
        out: dict = {}
        prev_n = len(state.get("messages") or [])
        logs: list[str] = []
        step = 0
        # `stream_agent` 顺带把**每个字**也写进日志（攒到换行写一行）——
        # 一次 LLM 调用那几十秒不再是黑屏。
        async for chunk in trace.stream_agent(main_agent, state, config, "主 agent",
                                              role=trace.AGENT):
            out = chunk
            msgs = chunk.get("messages") or []
            new = msgs[prev_n:]
            if not new:
                continue  # 第一个 chunk 是输入 state 本身，没有新消息
            prev_n = len(msgs)
            step += 1
            trace.dump_trace(f"主 agent 第 {step} 步", new, role=trace.AGENT)
            # 同一批调用**也进 state 的 log** —— 逐轮审计文件（`_run.md`）读的是它，
            # 只写 trace 文件的话，审计时还得两个文件对着看。
            for name, args in trace.tool_calls_of(new):
                line = f"[工具] 主 agent → {name}（{args}）"
                trace.note(line)
                logs.append(line)
        if not out:  # 理论上不会（stream 至少吐一次最终 state）—— 兜一下，别把状态弄丢
            # 🔴 **这条兜底必须说话**：`invoke` 会把整个 agent 循环**跑完**才返回 ——
            #    也就是**这一趟全程没有流式日志**，正是 `node_agent` 存在的全部理由要治的
            #    黑屏。原来这里一个字都不写 ✗ —— 真走到这条路上，读日志的人只会以为"卡住了"〔沿革 26〕。
            trace.note(
                "⚠️ 流式**一步都没吐** → 退回 `invoke` 兜底：**这一趟没有流式日志**，"
                "跑完才回来。正常不该发生 —— 见到这条要查 `stream` 为什么是空的。",
                kind="stream_empty",
            )
            return await main_agent.ainvoke(state, config)
        return {**out, "log": logs} if logs else out

    g.add_node("agent", node_agent)
    g.add_node("finalize", node_finalize)
    g.add_edge(START, "feed")
    g.add_edge("feed", "prepare")
    g.add_edge("prepare", "agent")
    g.add_edge("agent", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=InMemorySaver())
