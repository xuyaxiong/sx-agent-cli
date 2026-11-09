"""**日志 / 事件总线** —— 把原型跑的每一步写成权威记录，同时把结构化事件发给展示层。

日志落在**应用根**下的 `logs/proto-<时间戳>.log`（开发时是 `langgraph-lab/logs/`，
打包后是 exe 旁边的 `logs/`），同时打到控制台（简洁版）。
每次进程启动开一个新文件，不覆盖历史。

## 这个文件是什么

本模块同时是两样东西，中间隔着一层**事件总线**：

- **记录层** —— `logs/proto-*.log` 的唯一落盘者（`_recorder` / `_write`）。
  日志是**原始记录**，供人与 `_run.md` 附录消费。
- **事件源** —— 业务代码只调这一层的公开函数（`section` / `note` / `block` / `handoff` /
  `decision` / `cut` / `dump_trace` / `stream_agent`），它们**只发结构化事件**、不写文件。

```
业务代码 ──trace.*()──▶ 事件总线 _publish() ──┬──▶ 记录器 _recorder：落日志文件（权威记录）
                                            └──▶ 展示层（evals.chat 订阅）：自己排版上屏
```

**事件 = 记录层与展示层之间的唯一契约**；展示层不读日志文件。〔沿革 7〕

## 设计原则（不变量）

1. **事件的两种标签都是受控词汇** —— 打错一个字母**当场炸**，不许静默跳过
   （`_check_role` / `_check_kind` / `_publish`）：
   - `type`：事件的**形状**（八种之一，见 `_EVENT_TYPES`）—— 决定它带哪些字段；
   - `kind`：业务**语义**（`dispatch` / `run_begin` / `relay` …，见 `KINDS`）——
     展示层据此决定归到哪个角色，**不再靠子串匹配认行**（措辞一改就静默失配的根）。
2. **记录器不是"订阅者"** —— `_publish` 先调它、再送订阅者：展示层坏了最多是"看不见"，
   而记录层丢一条 = **权威记录破了个洞**，所以它单独兜、兜住后**把洞标出来**。〔沿革 11〕
3. **日志是原始记录** —— 记录层不截断、不收回已经流出去的字；要少看噪音就在**播报层**砍。
4. **落盘格式不是跨层契约** —— `_STREAM_MARK` / `_DECISION_HEAD` / `_NOTE_HEAD` 只是
   记录器自己的格式（而且名字私有）；展示层订阅事件、不读日志文件。
5. **`dump` 事件里带的是消息对象本身** —— `tool_call_id` 就在里面，工具配对、来源标注
   全部走结构，**不从渲染文本里反推结构**。

## 关键取舍

**① 展示层要的粒度：每个 super-step ＋ 每个字**（`stream_agent`）。
`invoke` 跑完才返回、全程黑屏（〔沿革 4〕），所以拆两层给：`values` 那一支给调用方做
逐步骤 dump，`stream` 事件给记录器落盘、展示层实时上屏。

**② 重试只一次，且只在"还没走完第一步"时**（`_stream_with_retry`）。
从头重跑 = `state` 干净、**副作用不干净**（〔沿革 21〕）。抖在那之前 = 什么都还没干，
重试是白赚；抖在那之后 = 已经动过手，**如实抛出去**比静默重做一遍强（代价见〔沿革 22〕）。

**③ 确定性拒绝不重试**（`upstream_rejected`）。4xx（除 408/429）是上游对**这份内容**的
拒绝 —— 原样重发会再被拒（〔沿革 28〕）。

## 遗留问题

- 落在"第二步之后"的抖动，这一轮**真的失败**了 —— 更彻底的"从最后一个走完的
  super-step 接着跑"还没做（〔沿革 23〕）。
- **同步文件 IO 还在事件循环里**（第四轮外审 §5.3）：`_write()` 每行 `write + flush`、
  没有锁；`skill_pack` 的 `read_text`（3 处）、`artifacts.write_text` 同理。
  ⚠️ 异步迁移（`graph.py` 沿革 33）把节点与工具都改成 `async def`，**顺带取消了
  "同步阻塞只影响一个会话"那个前提** —— 现在它们卡的是**整个事件循环**。
  ⚠️ **量级没实测**：单会话交互式下 flush 是微秒级、无感；多会话高频流式下是不是真成
  瓶颈**不知道** —— 要实测再定级，别照抄外审给的 P2。

## 沿革（改过什么、为什么改）

> 编号是**引用锚**（正文里写 `〔沿革 N〕`）—— **只追加、不改号**，新条目加在**末尾**。

1. **时间戳精确到毫秒 ＋ 年月日**（2026-10-30，用户定）。原话：*"日志实践精确到毫秒
   添加上年月日信息"*。以前只有 `HH:MM:SS` —— 一轮里同一秒内发生好几件事
   （工具调用 → 返回 → 判定），秒级看不出先后；跨天的日志也分不清是哪天。
2. **`BODY_ARGS` 只省"另有出处"的正文**（2026-10-30 用户纠正）。原来按"超过 120 字就省"
   一刀切，把 `delegate(task=...)` 也省成了 `<220字>`，而**那正是整个交接的核心**。
   用户：*"你怎么把这么关键的上下文交接信息给去掉了？？"*
3. **记录层不截断**（2026-10-30 用户定）。用户：*"这是优化agent表现的细节
   如果没有这些东西观察不到"* —— "平台注入了什么、agent 说了什么"是优化 agent 表现的
   **唯一观察窗口**。
4. **`stream_agent` 的由来**（2026-10-30）。`invoke` 会把整个 agent 循环跑完才返回，
   "模型→工具→模型→工具…"这一串**全程静默**。实测一次真实会话：主 agent 连调 8 个工具、
   **336 秒里日志一行不动** —— 而用户侧就是**黑屏**，分不清"在干活"还是"卡住了"。
5. **只发 `node='model'` 的 chunk**（2026-10-30）。`node='tools'` 是**工具内部的 LLM 调用**
   —— 实测就是试答那个结构化输出（它跑在 `delegate` 里面，却被外层的 stream 捕获），
   流出来的是**裸 JSON**：既不可读（用户：*"就打个 json 算啥"*），又和已经解开的展示重复。
6. **换了消息就 flush 缓冲区**（2026-11-02 实测踩到）。模型调工具前会先说一句、工具跑完
   再接着说，**两段之间没有换行符** —— 攒在同一个 `buf` 里就被黏成一句。
7. **本模块同时是事件源**（2026-11-03 架构定）。用户：*"原则上显示层的逻辑和核心业务逻辑
   不该是同一个层级。"* 公开函数从此**只发结构化事件**、不再直接写文件；**契约是事件，
   不是文本** —— 展示层不再读日志文件。`dump` 事件里带的是**消息对象本身**，
   工具配对、来源标注全走结构，旧的两条文本 hack（repr 反转义、按段 FIFO 配对）随之作废。
8. **角色从措辞里拆出来**（2026-11-03 e2e 实测）。`chat.py` 原来靠
   `"执行器" in ev["who"]` / `"run_task" in ev["tag"]` 认角色 —— 而 `who`/`tag`
   是**给人看的自由文本**（`执行器·lesson-prep`、`run_task 第 1 轮`），哪天改个措辞，
   展示层就**静默降级**成主 agent 的角色，没有任何报错。现在拆成两样：
   **`role` 是逻辑（枚举）**、**`who` 是显示名（随便改）**。
9. **`kind` 收成受控词汇**（2026-11-03 审核 F3）。原来 kind 是**自由字符串** —— 把
   `dispatch` 打成 `delegate`、或改个名，终端上那一行就**静默消失**，没有任何测试会红。
   加一个 kind 要同时登记进 `KINDS` ＋ 在 `evals/chat.py` **表态**（漏表态 `unit_console` 会红）。
10. **note 的前缀归记录器**（2026-11-03 审核 F1）。前缀原来是**业务侧**写进正文的
    （`f"finalize: 透传 …"`），展示层于是按**位置**切回来（`[len("finalize: "):]`）——
    只改文案措辞，上屏就被**静默吃掉 10 个字**（实测）。
11. **记录器不走订阅者那张网**（2026-11-03 审核 F4）。`_publish` 先调记录器、再送订阅者；
    展示层坏了最多是"看不见"，记录层丢一条却是**权威记录破了个洞** —— 所以它单独兜，
    兜住之后 `_mark_hole` **把洞标出来**（写明丢的是哪一条），**不混在"订阅者出错"里**。
12. **`handoff()` 的由来**（2026-11-03 用户定）。用户：*"你得加上关键日志 —— 这些在日志中
    得完整记录下来**便于复盘**。"* 摘要行让人一眼看到谁给谁 / 交了多少字 / 有没有结构化
    对象 / 跳名与方向（对应交接三问的那几条判据）。
13. **`cut()` 的由来**（2026-11-03 用户定）。用户：*"主动截断是否有显式标记，被截断的内容，
    有没有告知它能查到全文详情的方式方法。"* —— 标记在**文本里**（读的人看得见），
    这一行在**日志里**（复盘的人看得见），两者都要有。
14. **决策四格为什么是这四样**（2026-11-03 用户定）。用户：*"decide 那一段日志也得整理下
    格式，人看着也不清晰。既然是决策，很显然：**决策前提是什么、要决策的问题是什么、
    决策结果是什么**，有可能还要补充**下一步该干什么**。"* 顺序就是**复现这个决策**的顺序。
15. **决策段名只有一份**（2026-11-03 审核 F2）。记录器与展示层两边各抄一份的后果实测过：
    给那张表加一段，记录层整块决策从日志里**消失**、展示层**静默**少一段。
16. **`_DEC` 不再是跨层耦合**（2026-11-03）。它原来是 4，理由是"展示层要剥 2 格"；
    展示层改为订阅事件、自己排版后这条作废 —— 文件里留 4 格只是日志可读性的选择，
    与任何别层无关。
17. **文件格式不变**（2026-11-03 事件源重构）。`_recorder` 的落盘格式与重构前**逐字节
    一致**（`unit_decision` / `unit_stream_join` 守着）：日志是权威记录，老日志、`_run.md`
    附录、人的翻阅习惯都不断。记录层该怎么记还怎么记，只是不再为展示层的排版让路。
18. **`try/finally` 里 flush 半行**（2026-11-04 深度审计 §P1-4 实测踩到）。`buf` 是
    **生成器局部变量**，异常时原来**不 flush** —— "这半行说了什么"在日志里**一个字都没有**，
    而 `retry` note 只说"上面那半段作废"：读日志的人看不到被作废的是什么，
    **等于记录层替模型说了假话**（交接三问②：切了必须标）。
19. **`_STREAM_MARK` / `_DECISION_HEAD` 名字私有**（2026-11-04 复审 N1）。公开名本身就是
    "可以被 import"的邀请 —— 下一个消费者写 `from proto.trace import STREAM_MARK`
    照样跑得通，"展示层认记录层的文本"那类事故就会原样复发；私有名一写出来，
    就是**伸手进记录层**的信号（白盒测试除外，它们本来就该盯文件格式）。
20. **重试兜在 `_stream_with_retry`**（2026-11-04 复审 N2/R8）。这是**两条链唯一共用的缝**
    —— 主 agent 的循环、执行器的循环都从这过，放这儿两边一起有兜底，不会"改了一条忘了
    另一条"（那正是 R8 的形状：`executor.py` / `agent.py` 全文一个 try/except 都没有）。
    形状同 `llm.safe_structured`（重试一次 → 仍失败才退化）—— **先例在，别另立一套**。
21. **重试边界：只在"还没走完第一步"时重试**（2026-11-05 修）。原判定是布尔 `stepped`
    （"吐过东西"），被 `stream_mode="values"` 先吐的**输入 state chunk** 提前置真 ——
    重试对"第一步抖动"**从未生效**（守卫 `unit_stream_join` ④）。边界改成
    **数到第二个 values chunk**（第一个是输入 state；
    之后只有**改了 state 的步**才吐，所以"数到 2"＝"至少一步动过 state" —— 两条前提由
    `unit_stream_join` ⑦ 拿真图钉住）。**为什么不能从头重跑**：`state` 干净、
    **副作用不干净** —— 实测：重试会把这一轮已经做过的
    工具调用再做一遍：`delegate` 重跑 = 执行器再跑 60–120 秒 ＋ **再写一份产物**，
    而模型在重试的 `state` 里**看不到自己刚派过**（`Command` 的 update 没提交），
    于是**很可能再派一次** —— 用户等的时间翻倍、账单翻倍，产物库里多一份**没人指向的**
    产物（而且第二次的 `task` 是模型重写的，两份内容可能不一样）。
22. **重试边界的代价如实说**（2026-11-05）。落在"第二步之后"的抖动，这一轮就**真的失败**了
    —— 执行器那条有 `run_task` 的 fault 出口接着（回执【平台故障】→ 主 agent 原样重派）；
    主 agent 那条由入口兜。**数据**：216 个历史 run 里整轮炸 10 个，其中**真·瞬时的只有
    3 个**（其余是配额超限 / 代码 bug，重试也救不了），而这 3 个里**有 2 个死在"已经动过手
    之后"** —— 正是这里不再重试的位置。
23. **记一个没做的更彻底做法**（2026-11-05 用户定：*"先按做法二，做法三以后再试"*）。
    **从最后一个走完的 super-step 接着跑**，而不是从头来：`stream_mode="values"` 每一步都
    吐完整 `state`，把它留着，异常时拿它当输入再 `stream` 一次 —— 已经跑过的 `delegate` /
    `write_artifact` **不会再跑**（它们的 `ToolMessage` 和 `tasks` 更新都在那个 `state` 里），
    模型**看得见自己刚派过**，重放窗口缩到"失败的那一个 super-step" ——
    这样"第二步之后抖动"也能救回来，用户完全无感。
    **为什么现在不做**：① 要先验"拿一个 `create_agent` 子图的**中间 state** 再 `stream`
    一次"行为是否真如预期（step 计数 / pending writes / 消息会不会重）—— 得先做 spike，
    不能直接上；② 它和 `docs/solution.md` §五 **X4**（"不写草稿 / 断点外置"）是**同一族**，
    得先把边界说清 —— 这是**单轮、内存内**的续跑，不是把断点外置。
24. **"会话不死"只在交互式 CLI 成立**（2026-11-05 用户定）。`proto/__main__.py` 的
    `turn()` 按轮捕获；跑批入口（`evals/_child.py`）**不兜**、整轮记失败 ——
    用户：*"跑批断了就断了呗 真有啥影响 chat入口能兜住不就行吗？"* 跑批是**测试用途**，
    改成平台接住它**照样判红**（活没干完，断言过不了）。见 `docs/solution.md` §五 **X7**。
25. **判定那次 LLM 调用退休**（2026-11-05）。"完成没"改由平台**按结构算**，`judge`
    这个 kind 没人再发、删掉；两种判断（LLM 决策 / 平台判定）仍**共用 `decision()`
    这一条记录形状**（一套，别分叉），用 `result_by` 分那格是谁写的（`llm` / `platform`）
    —— 判据是用户那句：*"关键在于 LLM 参与了"*。
26. **~~`DECISION`~~ 段名表删**（2026-11-05）。它是**第二份段名表** ✗：`前提` / `问题` /
    `结果` / `下一步`，**不带 `【】`**、缩进另算 4 格 —— 而 `SECTION` 的注释写着"段名一律
    是 `【…】` 形式、**独占行首**，记录层靠这两条做确定性切分"，它把那条硬规矩
    **开了第二个口子**。现在段名在 `SECTION`（`dec_given` / `dec_q` / `dec_result` /
    共用的 `next`），顺序在 `HOPS["decision"]`，渲染在 `handoff.decision.render_decision`。
27. **决策块走唯一那条渲染路**（2026-11-05）。`_recorder` 原来**自己排一遍**（缩进 4 格），
    而终端那边（`chat._render_decision`）**又排一遍**（0/2 格）—— 同一件事两处定义，
    形状必然漂。现在两边都调 `render_decision()`，记录器只加"文件格式"的东西
    （时间戳前缀 + 段头）。
28. **`upstream_rejected` 的由来**（2026-11-06，外测 SX-07）。故障原来**一笼统**地当
    "基础设施抖动"处理 —— 重试一次、然后教主 agent"**原样再派**"。但 4xx（除 408/429）
    是上游对**这份内容**的确定性拒绝（如内容策略）：原样重发会**再被拒** ——
    重试是白烧一次（大生成很贵），"原样再派"是**错的话**（该说"换措辞/改内容"，
    见 `handoff.FaultKind`）。按 `status_code` 判、不用 provider 的类型：trace 层不认识
    openai（换一家 SDK 就失效），`status_code` 是 HTTP 语义，鸭子类型就够；
    说不准就当抖动（保守）。
29. **事件总线改成会话级**（2026-11-06）。起因：用户定 —— *"我这个会封装成一个库 面向
    多用户 所以所有东西都应该是面向 session 级的"*。原来 `_subs` 是**模块级** list：
    一个进程一份，谁订阅谁收到**所有人**的事件。而这是 **N2 修法开出的新接缝** ——
    N2 把"归属"从 `state`（每会话一份）搬到了事件总线（每进程一份），
    于是**"按会话隔离"这个性质丢了**。⇒ 三件：①订阅挂到 `Session.subs`（`subscribe`
    往**当前会话**加）；②`_publish` 给每个事件加 **`thread_id`**（**在这一层加**，
    不是八个发射器各带一个 —— 加漏一个就是"某类事件不知道属于谁"）；
    ③`subscribe()` **返回句柄**（`_Subscription.cancel()`，重复调安全）——
    会话结束要能摘掉订阅，否则长期运行**只增不减**。
    ⚠️ **日志还没按会话分**（`_fh` 仍是进程级、格式也没加会话列）：下一步。
    动它要碰 `_recorder` docstring 里"**格式逐字节一致**"那条，所以**单独一步评估**。
    ⚠️ 有效性自检：订阅退回模块级 list → `unit_decision` ⑪ 当场红（实测能看到
    A 收到 B 的事件）；删掉 `thread_id` 那行 → 同一条红。
30. **日志改成"每会话一个文件"**（2026-11-06，用户定：*"先按乙"*）。上一条留的尾巴：
    日志也要按会话分。两条路 —— **甲**（一个文件 ＋ 每行加会话列）／**乙**（每会话一个文件）
    —— 选了 **乙**，**全部理由是格式不用动**：`_recorder` 的 docstring 写着"格式与事件源
    重构前**逐字节一致**……日志是权威记录"，甲要碰那条，乙**一个字都不用改**。
    ⇒ `LOG_FILE` 仍是**默认会话**的文件（CLI / 跑批 / 单测都不开 `session()`，行为不变）；
    开了会话的走 `log_file()` —— **惰性建**（没写过日志的会话**不产生文件**），
    文件名带会话标识（`thread_id` **消毒**后截断 40 字：它由宿主给，直接拼进路径会被
    `/`、`..` 带出去）。`_fh` 从**模块级句柄**改成 `_fh()` 函数（当前会话的句柄）。
    ⚠️ 代价：`unit_stream_join` 原来靠 `trace._fh = buf` 猴补丁，4 处改成
    `session.default().log_fh`。
    ⚠️ 有效性自检：把 `_fh()` 改回模块级句柄 → `unit_decision` ⑫ 红（**而且第一版
    它是以 `FileNotFoundError` 崩的、不是红** —— 补了 `exists()` 才干净，同
    `unit_console` 那条"用 `find` 不用 `index`"的教训）。
31. **默认会话的日志也改惰性**（2026-11-06，用户：*"建那么多空文件夹干啥"*）。
    原来 `LOG_FILE.open("w")` 就跑在 **import 那一刻** —— **导入这个动作本身**在磁盘上
    建文件，而大多数进程（单测、只用会话的宿主）**一个字都不写它**。实测 `logs/` 里
    **2275 个文件、147 个是空的（6%）**，来源全是这条（`unit-handoff-form` 47、
    `unit-receipt` 42、`unit-registry` 36…… 那些单测猴补丁了句柄，真实文件从没被写过，
    **但建已经建了**）。
    ⇒ `log_fh = None`（"还没建"），`_fh()` 第一次写时才开 —— 于是**两条路一致**了
    （会话那条本来就是惰性的）。
    ⚠️ **代价**：惰性之后"这个进程还没写过日志"成了**正常状态**，`LOG_FILE.stat()` 会
    `FileNotFoundError` —— 加了 `trace.log_size()`（不存在 = 0），5 处读体积的地方
    换过去，3 处 `_capture` 的 `open` 加了存在性兜底。
    ⚠️ 有效性自检：把 `open()` 加回模块级 → `unit_decision` ⑭ 红（子进程 import 后
    磁盘上多出一个文件）。⑭ 是**开子进程实测**的 —— 同进程里看不出 import 的副作用。
32. **日志目录可配：`logs/` 只留真会话**（2026-11-06，用户：*"为什么 logs 里面有这么多
    文件啊 那些测试产生的你给放在项目 tmp/logs 下吧 原本那个 logs 我是要看 session 的
    日志"*）。实测：`logs/` **2309 个文件里 826 个是噪音**（670 单测 ＋ 148 空 ＋ 8 探针），
    把"翻真会话日志"这条路堵死了。⇒ 加 `PROTO_LOG_DIR` 开关，**由入口自己设**
    （16 个单测 ＋ `evals/run.py` 起子进程时）—— 谁是噪音只有入口知道，`trace` 猜不出来。
    ⚠️ **相对路径按"应用根"算，不按 cwd**（同 `config.app_root` 那条原则）。
    ⚠️ 守卫 `unit_feed_waiting` ⑮ 从"只守 `PROTO_LOG_TAG`"扩成"两个都守"；
    `unit_frozen_paths` **豁免**（它测的就是**默认路径**，设了就被顶掉 —— 而它写进
    临时目录，本来就不污染 `logs/`）。
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from proto import session
from proto.config import app_root
from proto.handoff.decision import RESULT_BY, render_decision

#: 日志落哪 —— 默认**应用根**下的 `logs/`（见 `proto.config.app_root`）：
#: 开发时是 `langgraph-lab/logs/`，打包后是 **exe 旁边的 `logs/`**。
#: ⚠️ 不能按 `__file__` 算：冻结后那是 `_internal/`，用户不该碰、重建还会被清掉。
#:
#: 🔴 **`logs/` 是给人看"真会话"的地方** —— 测试 / 跑批的日志是**噪音**，混进去会把
#: 翻会话日志的路堵死（实测：2309 个文件里 **826 个是噪音**：670 单测 ＋ 148 空 ＋ 8 探针）。
#: ⇒ 它们把 `PROTO_LOG_DIR` 指到 `tmp/logs/`（`tmp/` 本来就在 `.gitignore` 里）。
#: ⚠️ **由入口自己设**（16 个单测 ＋ `evals/run.py` 起子进程时），`trace` 只读这个开关 ——
#: 谁是噪音只有入口知道，这里猜不出来。〔沿革 32〕
_log_dir = os.getenv("PROTO_LOG_DIR", "").strip()
LOG_DIR = Path(_log_dir) if _log_dir else app_root() / "logs"
# ⚠️ **相对路径相对"应用根"，不相对 cwd** —— 同 `config.app_root` 那条原则：
#    "从哪双击都不该影响读不读得到配置"。这里同理：从哪跑测试都不该把日志扔到别处。
if not LOG_DIR.is_absolute():
    LOG_DIR = app_root() / LOG_DIR
LOG_DIR.mkdir(parents=True, exist_ok=True)

#: 文件名后缀 —— 时间戳只到**秒**，跑批时多个子进程同秒启动会**撞同一个文件**。
#: `evals/` 的子进程用 `PROTO_LOG_TAG=<case>-r<rep>` 区分；手工跑时不设，行为不变。
_TAG = os.getenv("PROTO_LOG_TAG", "").strip()
_SUFFIX = f"-{_TAG}" if _TAG else ""

#: **默认会话**的日志文件 —— CLI / 跑批 / 单测都不开 `session()`，它们用的就是它。
#: ⚠️ 开了会话就**各有各的文件**（见 `log_file()`）。**日志格式一个字没改** ——
#: 这是选"每会话一个文件"（而不是"一个文件加会话列"）的全部理由：`_recorder` 的
#: "格式逐字节一致"那条约束**不用动**。〔沿革 30〕
#: ⚠️ **它只是"文件会在哪儿"，不是"文件已经在了"** —— 真建出来是**第一次写日志**那一刻
#: （`_fh()` 惰性开）。原来这里就 `open("w")`：**`import` 这个动作本身**在磁盘上建文件，
#: 而大多数进程（单测、只用会话的宿主）一个字都不写它 —— 实测 `logs/` 里 **147 个空文件**。
#: 惰性之后**两条路一致**了（会话那条本来就是惰性的）。〔沿革 31〕
LOG_FILE = LOG_DIR / f"proto-{time.strftime('%Y%m%d-%H%M%S')}{_SUFFIX}.log"
session.default().log_file = LOG_FILE
# ⚠️ **别在这儿 open** —— `log_fh = None` 就是"还没建"，`_fh()` 会替我们开。


def log_size() -> int:
    """当前会话的日志**已经写了多少字节** —— **文件还没建 = 0**。

    ⚠️ 为什么要它：惰性之后"这个进程还没写过日志"成了**正常状态**，
    直接 `LOG_FILE.stat()` 会 `FileNotFoundError`。读体积的地方一律走这儿。
    """
    p = log_file()
    return p.stat().st_size if p.exists() else 0


def log_file() -> Path:
    """**当前会话**的日志文件 —— 第一次写日志时才建（没写过日志的会话**不产生文件**）。

    ⚠️ 文件名里会话那一段**要消毒**：`thread_id` 由**宿主**给（web 那边可能就是用户 ID），
    直接拼进路径会被 `/`、`..` 带出去。非 `[A-Za-z0-9_-]` 一律换 `_`，再截断。
    """
    s = session.current()
    if s.log_file is None:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", s.thread_id)[:40] or "session"
        s.log_file = LOG_DIR / f"proto-{time.strftime('%Y%m%d-%H%M%S')}-{safe}{_SUFFIX}.log"
    return s.log_file


def _fh() -> Any:
    """当前会话**打开着的**日志句柄 —— 没开就开（跟着 `log_file()` 一起惰性建）。"""
    s = session.current()
    if s.log_fh is None:
        s.log_fh = log_file().open("w", encoding="utf-8")
    return s.log_fh

BAR = "═" * 78


def stamp() -> str:
    """日志时间戳：**年月日 + 毫秒** —— 一轮里同一秒发生好几件事（工具调用 → 返回 → 判定），
    秒级看不出先后；跨天的日志也分不清是哪天。〔沿革 1〕
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


# ──────────────────────────── 事件总线 ────────────────────────────

#: 事件的八种**形状**。每种带什么字段，见各 emitter 与 `_recorder`。
_EVENT_TYPES = (
    "section", "note", "block", "stream", "dump", "decision", "handoff", "cut",
)

class _Subscription:
    """`subscribe()` 的返回 —— **拿它退订**。

    ⚠️ 为什么不是"返回 `fn` 让调用方自己 `remove`"：订阅者常常是**绑定方法**
    （`chat._on_event`），每次取属性都是**新对象** —— `list.remove` 找不到它。
    所以句柄**记着它在哪个列表、是哪一个槽**。
    """

    __slots__ = ("_owner", "_slot")

    def __init__(self, owner: list, slot: list) -> None:
        self._owner, self._slot = owner, slot

    def cancel(self) -> None:
        """退订。**重复调是安全的**（已经不在里面就什么都不做）。"""
        try:
            self._owner.remove(self._slot)
        except ValueError:
            pass


def subscribe(fn: Callable[[dict], None]) -> _Subscription:
    """订阅**当前会话**的事件总线。事件按发布顺序同步送达每个订阅者。

    🔴 订阅者抛异常**不会炸业务**：兜住并原样记进日志文件 ——
    展示层坏了最多是"看不见"，不能把正在跑的一轮弄死。

    ⚠️ **订阅挂在会话上，不是模块上**（原来 `_subs` 是模块级 list）：多会话共享进程时，
    A 的展示层收不到 B 的事件。〔沿革 29〕
    ⚠️ **返回句柄**（`_Subscription`）：会话结束要能摘掉订阅，否则长期运行**只增不减**。
    不接返回值也行（老调用点一个没改）。
    """
    slot = [fn]
    session.current().subs.append(slot)
    return _Subscription(session.current().subs, slot)


def _publish(ev: dict) -> None:
    """发一个事件：**先记录器**（唯一落盘者），再同步送**当前会话**的订阅者（展示层）。

    🔴 记录器**不走订阅者那张网**：展示层坏了最多是"看不见"，记录层丢一条却是
    **权威记录破了个洞** —— 所以它单独兜，且兜住之后 `_mark_hole` **把洞标出来**。〔沿革 11〕

    ⚠️ **`thread_id` 是这一层加的**（不是每个发射器自己带）：八个形状共用一处，
    加漏一个就是"某类事件不知道属于谁"。〔沿革 29〕
    """
    if ev.get("type") not in _EVENT_TYPES:
        # 形状只有那八种：打错一个字母，记录器与展示层都会**静默跳过**它 —— 宁可当场炸。
        raise ValueError(f"未知事件形状 type={ev.get('type')!r}：{list(_EVENT_TYPES)}")
    # 🔴 **会话标识**（多会话共享进程时，这是"这条事件属于谁"的唯一来源）。
    #    ⚠️ 默认会话的 `thread_id` 是空串 —— 单会话宿主（CLI / 跑批）行为不变。
    ev["thread_id"] = session.current().thread_id
    try:
        _recorder(ev)
    except Exception as e:  # noqa: BLE001 —— 不炸业务，但**必须把洞标出来**（见 _mark_hole）
        _mark_hole(ev, e)
    for slot in list(session.current().subs):
        fn = slot[0]
        try:
            fn(ev)
        except Exception as e:  # noqa: BLE001 —— 见 subscribe 的 docstring
            _fh().write(
                f"[{stamp()}] ⚠️ 事件订阅者出错"
                f"（{getattr(fn, '__qualname__', fn)}）：{type(e).__name__}: {e}\n"
            )
            _fh().flush()


def _mark_hole(ev: dict, e: Exception) -> None:
    """记录器渲染失败 → 在日志里**把洞标出来**（交接三问②：切了必须标）。

    ⚠️ 标洞本身也兜住（比如文件已关）—— 但**不许静默**：正常路径下这行会写进日志，
    读的人一眼知道**丢的是哪一条**，而不是"日志里怎么少了这一段"。
    """
    try:
        _write(
            f"[{stamp()}] ⚠️ **这一条没有落进日志**（记录器渲染失败）："
            f"type={ev.get('type')!r} kind={ev.get('kind')!r}"
            f" —— {type(e).__name__}: {e}"
        )
    except Exception:  # noqa: BLE001 —— 连标洞都写不进去，只能放弃；仍不炸业务
        pass


def _write(line: str = "") -> None:
    """写一行到日志文件（不自动换行缩进，原样写）。**只属于记录器**，不发事件。

    ⚠️ 私有：别处直接调它就绕过了事件总线 —— 展示层看不见那一行，而且是**静默**的。
    """
    _fh().write(line + "\n")
    _fh().flush()


# ──────────────────────────── 事件发射器（业务代码只调这一层） ────────────────────────────

#: **角色枚举** —— 展示层按它归版式（谁说的话、什么颜色），**不从 `who` 的措辞里猜**。
#:
#: 🔴 原来靠 `"执行器" in ev["who"]` / `"run_task" in ev["tag"]` 认角色 —— 而它们是
#: **给人看的自由文本**（`执行器·lesson-prep`、`run_task 第 1 轮`），改个措辞展示层就
#: **静默降级**成主 agent，没有任何报错（正是"禁止反结构化"那条铁律，只是挪进了字段里）。〔沿革 8〕
#:
#: 现在拆成两样：**`role` 是逻辑（枚举，逻辑依赖它）**、**`who` 是显示名（随便改）**。
#: 加新角色就加在这里 —— 加完 `ROLES` 会自动收紧，漏传的地方当场报错。
AGENT = "agent"
EXECUTOR = "executor"
ROLES = frozenset({AGENT, EXECUTOR})


def _check_role(role: Any, where: str) -> str:
    """角色只能是枚举值 —— 传错就**当场炸**，不许静默当成主 agent。"""
    if role not in ROLES:
        raise ValueError(
            f"{where} 的 role 必须是 {sorted(ROLES)} 之一，收到 {role!r}。"
            "（角色是逻辑，不是措辞：见 trace.AGENT / trace.EXECUTOR）"
        )
    return role


#: **kind 的受控词汇** —— 业务语义标签，展示层按它归角色 / 归并。
#:
#: 🔴 原来是**自由字符串** —— 把 `dispatch` 打成 `delegate`、或改个名，终端上那一行就
#: **静默消失**，没有任何测试会红。所以按 `role` 的办法收口：加一个 kind 就登记在这里，
#: 漏登记的地方**当场炸**。〔沿革 9〕
#: ⚠️ 加完还要去 `evals/chat.py` **表态**（上屏 / 有专门分支 / 只进日志）——
#: 漏表态 `unit_console` 会红。
KINDS = frozenset({
    # agent.delegate
    "dispatch", "supersede", "waiting", "delegate_failed",
    # executor.run_task
    "run_begin", "run_end", "executor_briefing", "nudge",
    # graph.prepare / finalize
    "feed", "relay",
    # llm.safe_structured（`llm_io` = 那次调用的 PROMPT / RESPONSE / 失败提示）
    # ⚠️ ~~`judge`~~ 已删：判定那次调用退休了，没人再发它。〔沿革 25〕
    "ask_main", "llm_io",
    # trace.stream_agent（`retry` = 这一轮抖动、已重试一次）
    "retry",
    # graph.node_agent（`stream_empty` = 流式**一步都没吐** → 退回 `invoke` 兜底，
    # 这一趟**全程没有流式日志** —— 正是那个函数要治的黑屏，所以必须让用户看见）
    "stream_empty",
    # executor.run_task（`run_fault` = 重试过仍失败，这一轮没跑成；
    # `ask` = 申报要信息 / `delivered` = 交付产物 → 这一轮结束，两者都是**结构事实**）
    "run_fault", "ask", "delivered",
})


def _check_kind(kind: str, where: str) -> str:
    """kind 只能是受控词汇里的值（空串 = 纯记录、不上屏）—— 打错就**当场炸**。"""
    if kind and kind not in KINDS:
        raise ValueError(
            f"{where} 的 kind={kind!r} 不在受控词汇里（{sorted(KINDS)}）。"
            "加新 kind 要同时：① 登记进 trace.KINDS；"
            "② 在 evals/chat.py 表态（上屏 / 有专门分支 / 只进日志）。"
        )
    return kind


def section(title: str, kind: str = "") -> None:
    """一个**段落头**（前后两道 `═══` 包着标题）。

    `kind`：业务语义标签（受控词汇，见 `KINDS`）—— 展示层按它归角色；
    不给就是"纯记录，不上屏"。
    """
    _publish({"type": "section", "kind": _check_kind(kind, "section"),
              "title": title, "ts": stamp()})


def note(text: str, kind: str = "") -> None:
    """一行记录（带时间戳）。`kind` 的语义同 `section`。

    ⚠️ **只发内容**：这一行在日志文件里的前缀（如 `relay` 的 `finalize: `）归**记录器**
    —— 业务侧带上它，展示层就得按位置切回来（措辞一改就静默吃掉几个字）。〔沿革 10〕
    """
    _publish({"type": "note", "kind": _check_kind(kind, "note"), "text": text, "ts": stamp()})


def block(label: str, text: str, *, kind: str = "", indent: str = "  ") -> None:
    """一个带标签的多行文本块（prompt / response / 交接正文）。"""
    _publish({"type": "block", "kind": _check_kind(kind, "block"), "label": label,
              "text": text, "indent": indent, "ts": stamp()})


def kv(label: str, **fields: Any) -> None:
    """写一行键值对（形状上是一行 note）。"""
    inner = " | ".join(f"{k}={v}" for k, v in fields.items())
    _publish({"type": "note", "kind": "", "text": f"{label} | {inner}", "ts": stamp()})


def handoff(
    hop: str,
    frm: str,
    to: str,
    text: str,
    *,
    structured: str = "",
    full: bool = False,
) -> None:
    """**一次交接的完整记录** —— 一行摘要（＋可选全文）。〔沿革 12〕

    摘要行让复盘的人**一眼看到**：
      - **谁给谁、交了多少字** → 判据①（内容）
      - **有没有结构化对象** → 判据④（结构 ＋ 模板两层分离）
      - **跳名与方向** → 判据⑤（这一跳是平台硬拼还是 LLM 转述）

    截断（判据②）由**切的那一方**自己报（`cut()`）—— 不在这里事后解析文本。
    膨胀（判据③）看每轮的上下文读数（`harness.snapshot` / CLI 的 `📊 CONTEXT` 行）。

    ⚠️ `full=True` 只在**别处没有 dump 过**这一跳时用（否则日志里两份）。
    """
    _publish({"type": "handoff", "kind": "", "hop": hop, "frm": frm, "to": to,
              "text": text, "structured": structured, "full": full, "ts": stamp()})


def decision(
    who: str,
    *,
    q: str = "",
    given: Any = (),
    result: Any = (),
    nxt: str = "",
    result_by: str = "llm",
) -> None:
    """记一次**判断**：前提 → 问题 → 结果 → 下一步（段名与顺序见 `handoff.decision`）。

    🔴 **两种判断共用这一条记录形状**（一套，别分叉）—— 它们问的是同一件事
    （**谁基于什么、得出了什么、接下来干什么**），只是"填它的人"不同：

      - **LLM 决策**（`agent._ask_main` 的试答）—— 它是**交接**（两端都是）；
      - **平台的结构判定**（`executor._verdict` 的「任务完成没」）—— **不是交接**
        （没有 LLM 参与 —— 用户判据："关键在于 LLM 参与了"），但四格一样成立：
        前提 = 平台看得见的结构事实、结果 = 三态 ＋ **依据**。〔沿革 25〕

    🔴 发的是**结构化事件**（字段原样在 `ev` 里），渲染成什么样是**记录器与展示层各自的事**
    —— 展示层不再从渲染文本里反解析。

    `given` / `result` 传**列表**（逐项一行）→ 渲染成 `- ` 列表；传字符串 → 一行。
    ⚠️ 列表化不是装饰：判定那段原来直接把 `j.missing` 打出来，人看到的是
    **Python repr** `['尚未收到…', '教案文本文件产物为空…']` —— 括号引号全是噪音。

    `result_by`：**【结果】那一格是谁写的** —— `"llm"`（试答，要标来源）／
    `"platform"`（「任务完成没」按结构定，平台自己的结论，不标）。它决定那格用哪个段名，
    见 `handoff.decision.RESULT_BY`（段名要回答"这是谁的话"，借错了就是假话）。
    """
    if result_by not in RESULT_BY:
        # 打错一个字母就**静默**把平台的结论标成"它说的" —— 宁可当场炸（同 role / kind 那两条）。
        raise ValueError(
            f"decision 的 result_by={result_by!r} 不在 {sorted(RESULT_BY)} 里（见 handoff.decision.RESULT_BY）"
        )
    _publish({"type": "decision", "kind": "", "who": who, "q": q,
              "given": given, "result": result, "next": nxt,
              "result_by": result_by, "ts": stamp()})


def cut(where: str, kept: int, total: int, *, see: str = "") -> None:
    """**切了一刀就报一刀** —— 由切的那一方自己调，不靠事后从文本里解析。

    标记在**文本里**（读的人看得见），这一行在**日志里**（复盘的人看得见），
    两者都要有（交接三问②）。〔沿革 13〕

    - `where`：**切的是哪一份**，要说人话（谁看到的东西被切了、原件动没动）。
    - `kept` / `total`：**实测**字数（不是预算）—— 报意图不报事实，改天就静默不符。
    - `see`：**补一句给读日志的人**（"全文在哪"／"为什么没有全文"）—— 原样接在 `｜` 后面，
      所以措辞由调用点负责（要写"全文：…"就自己写）。
    """
    _publish({"type": "cut", "kind": "", "where": where, "kept": kept,
              "total": total, "see": see, "ts": stamp()})


def dump_trace(tag: str, messages: list, *, role: str) -> None:
    """把一整条消息轨迹**作为事件**发出去（执行器内部发生了什么，全在这）。

    🔴 事件里带的是**消息对象本身**（不是渲染好的行）—— 展示层靠 `tool_call_id`
    做工具配对、靠 `additional_kwargs` 认平台注入，**不从渲染文本里反推结构**。

    `role`：说话的是谁（`trace.AGENT` / `trace.EXECUTOR`），**必填** ——
    展示层按它归版式。`tag` 只是给人看的描述（`run_task 第 1 轮`），
    **逻辑不许从 tag 里认东西**（`"run_task" in tag` 那种写法实测踩过）。〔沿革 8〕
    """
    _check_role(role, "dump_trace")
    _publish({"type": "dump", "kind": "", "role": role, "tag": tag,
              "messages": list(messages), "ts": stamp()})


# ──────────────────────────── 消息渲染（记录器专用） ────────────────────────────

#: **正文类参数**：只留字数。
#:
#: 🔴 **只有它们该省**：`write_artifact(content=...)` 的正文
#: **另有出处**（产物文件），灌进日志纯属浪费。〔沿革 2〕
#:
#: ⚠️ **其余参数一律照原样给全** —— 判据跟显示层那条一样：
#: **全文另有出处才省，没有就全给**。
BODY_ARGS = frozenset({"content"})


def brief_args(args: Any, limit: int = 120) -> str:
    """工具参数的一行摘要 —— **只有 `BODY_ARGS` 里的正文类参数**会被压成字数。"""
    if not isinstance(args, dict):
        return str(args)
    parts = []
    for k, v in args.items():
        text = str(v)
        parts.append(f"{k}=<{len(text)}字>" if k in BODY_ARGS and len(text) > limit else f"{k}={text!r}")
    return ", ".join(parts)


def tool_calls_of(messages: list) -> list[tuple[str, str]]:
    """从消息里抽出**全部**工具调用 —— `(名字, 参数摘要)`，按发生顺序。

    主 agent 的 `read_artifact` 这类调用，原来在日志里查不到（只有执行器的轨迹被 dump），
    只能靠"消息类型对不上"反推。这个函数是给日志补上那一段的。
    """
    out: list[tuple[str, str]] = []
    for m in messages:
        for c in getattr(m, "tool_calls", None) or []:
            out.append((str(c.get("name")), brief_args(c.get("args") or {})))
    return out


def render_message(m) -> str:
    """把一条 LangChain 消息渲染成可读文本（**记录器落盘用**）。

    🔴 **这里不截断**：日志是**原始记录**，而"平台注入了什么、agent 说了什么"正是
    **优化 agent 表现的唯一观察窗口** —— 要少看噪音就在**播报层**砍，别在记录层砍。〔沿革 3〕

    ⚠️ **AI 的话只写头部，不重复正文** —— 正文已经由 `stream_agent` 逐行流过日志了。
    """
    kind = type(m).__name__
    content = str(m.content)
    parts = [f"{kind}({len(content)}字)"]
    # 🔴 **平台注入的标记**（`proto.handoff.INJECTED`）—— 透传注入的是 AIMessage，
    # 内容是**执行器的原话**；不写出来，读日志的人会以为那是主 agent 说的。
    tag = (getattr(m, "additional_kwargs", None) or {}).get("injected_by")
    if tag:
        parts.append(f"    injected_by={tag}")
    if content.strip() and kind != "AIMessage":
        # `!r` 只为**压成一行**（日志一行一条），不是为了截断。
        parts.append(f"    content={content!r}")
    for c in getattr(m, "tool_calls", None) or []:
        parts.append(f"    tool_call={c.get('name')} args={brief_args(c.get('args') or {})}")
    return "\n".join(parts)


def stream_agent(graph, state, config, who: str, *, role: str):
    """跑一个 agent 图，**每一步 + 每个字**都录下来 —— 边跑边发 `stream` 事件。

    Yields：每个 super-step 的完整 state（`stream_mode="values"` 那一支）。
    ⚠️ **第一个 chunk 是输入 state 本身**（先于任何 super-step；之后只有改了 state 的
    步才吐）—— 消费方别把它当成"走了一步"（`_stream_with_retry` 的重试边界靠这条，见那里）。

    🔴 **为什么要有它**：`invoke` 会把整个 agent 循环跑完才返回，于是"模型→工具→模型→
    工具…"这一串**全程静默**，用户侧就是**黑屏**，分不清"在干活"还是"卡住了"。〔沿革 4〕

    两层一起给：
      - **每个 super-step** → `values` 那一支，调用方拿去做逐步骤 dump
      - **每个字** → `stream` 事件（攒到换行发一行），记录器落盘、展示层实时上屏

    ⚠️ **前提：所有 `AIMessage` 都从这里流过** —— 所以记录器对 AI 的话
    **只写头部、不再重复正文**（正文已经在上面流出来了）。改调用点时记得这条。

    ⚠️ 多个执行器**并行**时，它们的事件会交错 —— 所以 `who` 要能分辨是谁
    （主 agent / 执行器·`<技能名>`）。

    ⚠️ **只发 `node='model'` 的 chunk** —— 工具内部的结构化调用（判定 / 试答）
    流出来是裸 JSON，既不可读、又和已解开的展示重复。〔沿革 5〕

    `role`：`trace.AGENT` / `trace.EXECUTOR`，**必填** —— 展示层按它归版式。
    `who` 只是显示名（`执行器·lesson-prep`），**随便改，不影响任何逻辑**。
    """
    # 🔴 校验在**生成器外面**：漏传 / 传错立刻炸，而不是等第一次 next()。
    return _stream_with_retry(graph, state, config, who, _check_role(role, "stream_agent"))


def upstream_rejected(e: BaseException) -> bool:
    """这个异常是不是「上游**确定性拒绝**」—— 4xx（除 408/429）？

    🔴 **为什么要有它**：4xx（除 408/429）是上游对**这份内容**的拒绝（如内容策略）——
    原样重发会**再被拒**：重试是白烧一次（大生成很贵），"原样再派"是**错的话**
    （该说"换措辞/改内容"，见 `handoff.FaultKind`）。〔沿革 28〕

    ⚠️ **按 `status_code` 判、不用 provider 的类型**：trace 层不认识 openai（换一家 SDK
    就失效）；`status_code` 是 HTTP 语义，鸭子类型就够。
    ⚠️ **说不准就当抖动**（保守，同现状）：没有 `status_code` 的异常（网络/超时/网关）
    照旧重试一次；**408（请求超时）/ 429（限流）是瞬时的**，也照旧重试
    （而且 SDK 自己已内重试过一轮，我们这一轮是第二道）。
    """
    code = getattr(e, "status_code", None)
    return isinstance(code, int) and 400 <= code < 500 and code not in (408, 429)


async def _stream_with_retry(graph, state, config, who: str, role: str):
    """跑 `_stream_agent`；基础设施抖动就重试一次（**边界见下**），仍失败才抛。

    🔴 **为什么兜在这一层**：这是**两条链唯一共用的缝** —— 主 agent 的循环、执行器的
    循环都从这过，放这儿两边一起有兜底，不会"改了一条忘了另一条"（`executor.py` /
    `agent.py` 全文一个 try/except 都没有）。〔沿革 20〕
      - **降级不在这里**：两条链该怎么退化不一样（执行器 → `run_task` 落"这轮没跑成"
        交主 agent 决定；主 agent → 由**入口**兜）。这里只做最保守的一步：
        重试一次，然后把异常**原样抛给调用方**。
      - 形状同 `llm.safe_structured`（重试一次 → 仍失败才退化）—— **先例在，别另立一套**。

    ⚠️ **"会话不死"只在交互式 CLI 成立**（`proto/__main__.py` 的 `turn()` 按轮捕获）；
    跑批入口（`evals/_child.py`）**不兜**，整轮记失败 —— 🔴 **这是定的，不是缺口**：
    跑批是**测试用途**，改成平台接住它**照样判红**（活没干完，断言过不了）。
    别再往 `node_agent` 里加兜底了 —— 见 `docs/solution.md` §五 **X7**。〔沿革 24〕

    **重试的边界：只在"还没走完第一步"时重试。**

    🔴 **从头重跑 = `state` 干净，副作用不干净**（实测见〔沿革 21〕）—— 所以第一个
    super-step 走完就不再重试：抖在那之前 = 什么都还没干，重试是**白赚**；抖在那之后 =
    已经动过手，**如实抛出去**比静默重做一遍强。
    ⇒ 边界是**数到第二个 values chunk**（`_stream_agent` **只在每步结束时** `yield`，
    流式的字是发 `stream` 事件、不走 yield；第一个 chunk 是输入 state，之后只有
    **改了 state 的步**才吐，所以"数到 2"＝"至少一步动过 state" —— 两条前提由
    `unit_stream_join` ⑦ 拿真图钉住）。

    ⚠️ **代价如实说**：落在"第二步之后"的抖动，这一轮就**真的失败了**（〔沿革 22〕）
    —— 执行器那条有 `run_task` 的 fault 出口接着；主 agent 那条由入口兜。

    📌 **更彻底的做法（从最后一个走完的 super-step 接着跑）没做** —— 见〔沿革 23〕。

    ⚠️ 已经流出去的字**不收回**（记录层是原始记录）—— 所以重试前先发一条 `retry` 事件，
      把"上面那半段作废、现在重来"说破；不然读日志的人会以为模型把话说重了。
    """
    #: 已经吐出去的 values chunk 数 —— ⚠️ **第一个是"输入 state 本身"**（真实图在任何
    #: super-step 执行之前先吐它；之后**只有改了 state 的步才吐** —— 两条都由
    #: `unit_stream_join` ⑦ 拿真图钉住）。所以"至少一步动过 state"＝数到 **2**。
    #: 🔴 原来这里是布尔 `stepped`（"吐过东西"）—— 被那个输入 chunk 提前置真，
    #: 重试对"第一步抖动"**从未生效**（守卫 `unit_stream_join` ④）。〔沿革 21〕
    chunks_seen = 0
    for attempt in (1, 2):
        try:
            async for chunk in _stream_agent(graph, state, config, who, role):
                chunks_seen += 1
                yield chunk
            return
        except Exception as e:  # noqa: BLE001 —— 抖动的种类不可穷举（网络/超时/限流/网关）
            # 🔴 **确定性拒绝不重试**：4xx（除 408/429）是上游对**这份内容**的拒绝 ——
            #    原样重发会再被拒，重试只是白烧一次（大生成很贵）。照实抛给调用方，
            #    由它按 `fault_kind="rejected"` 说人话（见 `upstream_rejected`）。〔沿革 28〕
            if attempt == 2 or chunks_seen >= 2 or upstream_rejected(e):
                raise
            # ⚠️ **走 `note()` 不走 `_publish()`**（第三轮外审 P3-4）：这一处原来直调
            #    `_publish` 并**写死** `kind="retry"` —— 一个**真 kind 值**却绕过了
            #    `_check_kind`。改名 / 打错（`retrry`）时校验抓不到，只有展示层那道
            #    运行时自检会警告。其余直调处写的都是 `kind=""`（纯记录，安全），
            #    只有这一个是"真值走小路"。
            note(f"⚠️ {who} 这一轮**基础设施抖动**（{type(e).__name__}: {e}）"
                 "—— 已重试一次（上面流出去的那半段作废）", kind="retry")


async def _stream_agent(graph, state, config, who: str, role: str):
    buf = ""
    cur_id: str | None = None      # 当前这条 AI 消息的 id —— 换了消息就把 buf 落盘

    def flush_line(line: str) -> None:
        if line.strip():
            _publish({"type": "stream", "kind": "", "role": role, "who": who,
                      "text": line, "ts": stamp()})

    # 🔴 `try/finally` 是**必须的**：`buf` 是**生成器局部变量**，异常时不 flush 的话
    #    "这半行说了什么"在日志里**一个字都没有** —— 而 `retry` note 只说"上面那半段作废"，
    #    读日志的人看不到被作废的是什么，**等于记录层替模型说了假话**（判据②：切了必须标）。〔沿革 18〕
    #    ⚠️ 重试**不收回**已经发出去的 `stream` 事件（记录层是原始记录）——
    #    所以那半行会留在日志里，紧跟着一条 `retry` note 说它作废。
    try:
        async for mode, payload in graph.astream(state, config, stream_mode=["values", "messages"]):
            if mode == "messages":
                chunk, meta = payload
                # 🔴 **只发 agent 自己说的话**（`node='model'`）。
                #
                # `node='tools'` 是**工具内部的 LLM 调用** —— 实测就是试答那个
                # **结构化输出**（它跑在 `delegate` 里面，却被外层的 stream 捕获）。
                # 流出来的是**裸 JSON**：既不可读、又和**已经解开的展示重复** ——
                # 它们的结论在 `decision` 事件里已经是人话，这里再发一遍只是噪音。〔沿革 5〕
                if meta.get("langgraph_node") != "model":
                    continue
                # 🔴 **换了消息就先把缓冲区落盘**：模型调工具前会先说一句、工具跑完再接着
                #    说，**两段之间没有换行符** —— 攒在同一个 `buf` 里就被黏成一句。〔沿革 6〕
                #    判据用 `chunk.id`：同一条 AI 消息的流式 chunk 共享 id，换一次调用就换 id。
                # ⚠️ 有些 provider 不给 id（`None`）—— 那时**保持原行为**，不要瞎猜边界。
                cid = getattr(chunk, "id", None)
                if cid and cur_id and cid != cur_id:
                    flush_line(buf)
                    buf = ""
                if cid:
                    cur_id = cid

                text = _chunk_text(chunk)
                if not text:
                    continue
                buf += text
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    flush_line(line)
                continue
            yield payload
    finally:
        # 收尾：最后一行没有换行符。**异常时也走这里**（见上面那条 try/finally 的理由）——
        # 攒在 `buf` 里的那半行照发，读日志的人至少知道"它说到哪儿断的"。
        flush_line(buf)


def _chunk_text(chunk) -> str:
    """从一个流式 chunk 里取纯文本（内容可能是 str，也可能是分块列表）。"""
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(p.get("text") or "") if isinstance(p, dict) else str(p) for p in content
        )
    return ""


def close() -> None:
    """关掉**当前会话**的日志句柄（没开过就什么都不做）。"""
    s = session.current()
    if s.log_fh is not None:
        s.log_fh.close()
        s.log_fh = None


# ──────────────────────────── 记录器：唯一的文件落盘者 ────────────────────────────

#: 流式行在**日志文件**里的前缀 —— ⚠️ 只是记录器自己的落盘格式，**不是跨层契约**
#: （展示层订阅事件，不再读日志文件）。
#:
#: 🔴 **名字也私有**：公开名本身就是"可以被 import"的邀请 —— 下一个消费者写
#: `from proto.trace import STREAM_MARK` 照样跑得通，"展示层认记录层的文本"那类事故
#: 就会原样复发。私有名一写出来，就是**伸手进记录层**的信号（白盒测试除外，
#: 它们本来就该盯文件格式）。〔沿革 19〕
_STREAM_MARK = "⟨生成·"

#: 决策块的**首行前缀**（文件格式用）。同上：私有。
_DECISION_HEAD = "🧭 决策"

#: 某些 `kind` 的 note 在**文件里**的前缀 —— 文件格式的一部分，归记录器。
#:
#: 🔴 前缀原来是**业务侧**写进正文的（`f"finalize: 透传 …"`），展示层于是按**位置**
#: 切回来（`[len("finalize: "):]`）—— 只改文案措辞，上屏就被**静默吃掉 10 个字**（实测）。
#: 现在业务侧只发内容，前缀归记录层。〔沿革 10〕
_NOTE_HEAD = {"relay": "finalize: "}

#: **一次 LLM 决策，人要看的四样** —— 顺序就是**复现这个决策**的顺序：
#:     前提（它看到了什么）→ 问题（它要回答什么）→ 结果（它答了什么）→ 下一步（平台据此干什么）
#:
#: ⚠️ **段名只有这一份**：记录器与展示层**都从这里取**（各自排版）—— 两边各抄一份的
#: 后果实测过：给这张表加一段，记录层整块决策从日志里**消失**、展示层**静默**少一段。〔沿革 15〕
#: ⚠️ ~~`DECISION`~~ 已删 —— 它是**第二份段名表** ✗（`前提` / `问题` / `结果` / `下一步`，
#: **不带 `【】`**、缩进另算 4 格），把"段名一律 `【…】` 形式、独占行首"那条硬规矩
#: **开了第二个口子**。现在段名在 `SECTION`（`dec_given` / `dec_q` / `dec_result` /
#: 共用的 `next`），顺序在 `HOPS["decision"]`，渲染在 `handoff.decision.render_decision`。〔沿革 26〕

#: 决策块的缩进 —— **记录层自己的排版**，与任何别层无关。
#: ⚠️ 它曾是 4，理由是"展示层要剥 2 格"—— 展示层改为订阅事件、自己排版后，这条跨层
#: 耦合**作废**；文件里留 4 格只是日志可读性的选择。〔沿革 16〕
_DEC = "    "


def _recorder(ev: dict) -> None:
    """把事件渲染成**日志文件的一行（或一块）**—— 文件格式的唯一出处。

    🔴 格式与事件源重构前**逐字节一致**（`unit_decision` / `unit_stream_join` 守着）：
    日志是权威记录，老日志、`_run.md` 附录、人的翻阅习惯都不断。〔沿革 17〕
    """
    ts = ev["ts"]
    t = ev["type"]
    if t == "section":
        _write()
        _write(f"{BAR}\n[{ts}] {ev['title']}\n{BAR}")
    elif t == "note":
        _write(f"[{ts}] {_NOTE_HEAD.get(ev.get('kind') or '', '')}{ev['text']}")
    elif t == "block":
        _write(f"[{ts}] {ev['label']}:")
        for line in str(ev["text"]).splitlines() or [""]:
            _write(f"{ev['indent']}{line}")
    elif t == "stream":
        _write(f"[{ts}] {_STREAM_MARK}{ev['who']}⟩ {ev['text']}")
    elif t == "handoff":
        _write(f"[{ts}] 📤 交接 · {ev['hop']}（{ev['frm']} → {ev['to']}）：{len(ev['text'])} 字"
               f"｜结构化：{ev['structured'] or '（纯文本）'}")
        if ev["full"]:
            _recorder({"type": "block", "label": f"交接全文 · {ev['hop']}",
                       "text": ev["text"], "indent": "  ", "ts": ts})
    elif t == "cut":
        _write(f"[{ts}] ✂️ 截断 · {ev['where']}：保留 {ev['kept']} 字 / 原文 {ev['total']} 字"
               + (f"｜{ev['see']}" if ev.get("see") else ""))
    elif t == "dump":
        messages = ev["messages"]
        _write(f"[{ts}] {ev['tag']} 消息轨迹（{len(messages)} 条）:")
        for i, m in enumerate(messages):
            _write(f"  [{i}] {render_message(m)}")
    elif t == "decision":
        # 🔴 **走唯一那条渲染路**：这里原来**自己排一遍**（缩进 4 格），而终端那边
        #    （`chat._render_decision`）**又排一遍**（0/2 格）—— 同一件事两处定义，
        #    形状必然漂。现在两边都调 `render_decision()`，**这里只加"文件格式"的东西**
        #    （时间戳前缀 + 段头）。〔沿革 27〕
        body = "\n".join(f"{_DEC}{ln}" for ln in render_decision(ev).splitlines())
        _write(f"[{ts}] {_DECISION_HEAD} · {ev['who']}\n{body}")


# ⚠️ 记录器**不订阅**：`_publish` 直接先调它（顺序：记录器 → 订阅者）——
# 这样它出错能走 `_mark_hole`（把洞标出来），而不是混进"订阅者出错"里。〔沿革 11〕
