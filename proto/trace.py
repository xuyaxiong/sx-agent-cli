"""**事件总线** —— 业务代码只调这一层；事件是记录层与展示层之间的**唯一契约**。

## 这个文件是什么

**发射器**（`section` / `note` / `block` / `kv` / `handoff` / `emit` / `cut` / `dump_trace`）
只发**结构化事件**、不写文件；`_publish` 把它们送给**记录器**（落日志）与**当前会话的
订阅者**（展示层）。

```
业务代码 ──trace.*()──▶ 事件总线 _publish() ──┬──▶ 记录器（proto/recorder.py）：落日志文件
                                            └──▶ 展示层（app.chat 订阅）：自己排版上屏
```

⚠️ **另外两半都拆出去了**（2026-11-09）：**落盘**住 `proto/recorder.py`、
**跑图**（`stream_agent` 那 5 个）住 `proto/stream.py` —— 本模块从此**只做总线**，
**不认识任何领域词汇**，只依赖 `session`。〔沿革 33〕〔沿革 34〕

## 设计原则（不变量）

1. **事件的两种标签都是受控词汇** —— 打错一个字母**当场炸**，不许静默跳过
   （`check_role` / `_check_kind` / `_publish`）：
   - `type`：事件的**形状**（八种之一，见 `_EVENT_TYPES`）—— 决定它带哪些字段；
   - `kind`：业务**语义**（`dispatch` / `run_begin` / `relay` …，见 `KINDS`）——
     展示层据此决定归到哪个角色，**不再靠子串匹配认行**（措辞一改就静默失配的根）。
2. **总线认识形状，不认识词表** —— `_EVENT_TYPES` 里列着 `decision` 这个**形状**，
   但"`result_by` 能取哪些值"是**领域词汇**，归 `handoff.decision`（**发射器也在那儿**）。
   ⇒ 本模块**不许 import `proto.handoff.*`**。〔沿革 33〕
3. **记录器不是"订阅者"** —— `_publish` 先调它、再送订阅者：展示层坏了最多是"看不见"，
   而记录层丢一条 = **权威记录破了个洞**，所以它单独兜、兜住后**把洞标出来**。〔沿革 11〕
   ⚠️ 记录器是**槽位**（`install_recorder`），由装配处装 —— 本模块**不 import 它**
   （否则又回到"基础能力传递依赖领域词表"）。
4. **日志是原始记录** —— 记录层不截断、不收回已经流出去的字；要少看噪音就在**播报层**砍。
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
   加一个 kind 要同时登记进 `KINDS` ＋ 在 `app/chat.py` **表态**（漏表态 `unit_console` 会红）。
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
33. **拆出记录器：本模块只剩总线（＋传输）**（2026-11-09）。起因是量了一次依赖分层 ——
    本模块落在 **L2**（比 `llm` 和整个 `handoff/*` 都高），唯一原因是一条边
    `trace → handoff.decision`（记录器要渲染决策块）。而记录器**是个视图**，
    视图依赖领域词汇表**是对的** —— 错的是它和**总线**住在一个文件里，于是
    `llm → trace → handoff.decision`：**基础能力传递依赖领域词表**，而 `handoff`
    也就永远抽不出去（它自己的 `briefing` / `ledger` 又依赖总线）。
    ⇒ 拆成三份：**总线**（留这儿，只依赖 `session`）／**记录器**
    （`proto/recorder.py` —— 视图，依赖 `handoff.decision`）／**`decision()` 发射器**
    （归 `handoff.decision`：**领域事件的形状归领域包**）。
    🔴 **`_publish` 对记录器的处置一个字没改**（还是单独 try/except ＋ `_mark_hole`）——
    变的只是"谁把它接上去"：从**硬接**改成**槽位 ＋ 装配处安装**（`install_recorder`）。
    ⚠️ 代价如实说：**忘了 import `proto.recorder` = 日志静默没有** —— 所以配了守卫
    （`unit_decision` ⑮ 盯三个入口），**接线靠守卫，不靠记性**。
    ⚠️ 沿革 1 / 17 / 19 / 30 / 31 / 32 讲的东西**现在住 `proto/recorder.py`**
    （编号**不动** —— 那是引用锚，别处还按号指过来）。
    ⚠️ **传输没动**（`stream_agent` 那 5 个）：它不造成层次错误，本轮不碰。
34. **传输也拆出去了 → `proto/stream.py`**（2026-11-09）。沿革 33 里"本轮不碰"的那个，
    用户定：*"一个文件为啥要装两个完全不同的事啊"*。
    ⚠️ **搬它的理由只有一条：一个模块一个职责** —— 审计给的三条**都不成立**（核过）：
    ① "记录层知道了模型超时"——本模块从 `config` 只拿 `app_root`；
    ② "和 `llm.safe_structured` 是同一类策略、必然分叉"——**方向反了**，两处本来就该
    不一样（那条路上的 400 **就是它自己的抖动**，分类了反而掐掉唯一有效的重试）；
    ③ "`stream_agent` 成了事实上的 LLM 入口"——那是描述，不是缺陷。
    ⇒ `_check_role` 顺带**提成公开的 `check_role`**（新模块要用，跨模块别用私有名）。
    ⚠️ 前史（重试边界 / 上游拒绝 / 流式 dump）见沿革 4 / 5 / 6 / 18 / 20 / 21 / 22 / 23 / 28
    —— **编号不动**，那些条目讲的东西现在住 `proto/stream.py`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from proto import session

#: **记录器槽** —— 由装配处装（`proto/recorder.py` 在 import 时自注册）。
#: ⚠️ **默认空**：库用户 / 不落盘的宿主不装，行为就是"没有日志文件"（原则 3、取舍 ①）。
_sink_record: Callable[[dict], None] | None = None
_sink_write: Callable[[str], None] | None = None


def install_recorder(*, record: Callable[[dict], None], write: Callable[[str], None]) -> None:
    """**装上记录器** —— 总线不认识它，只认这两个函数。

    - `record(ev)`：把一条事件渲染成日志行（**权威记录**，出错会被 `_mark_hole` 标出来）；
    - `write(line)`：写一行原文（**标洞**和"订阅者出错"用 —— 它们不走渲染）。

    🔴 **为什么是槽位而不是 import**：import 它 = 总线认识领域词汇表，于是
    `llm → trace → handoff.decision`（基础能力传递依赖领域词表）。〔沿革 33〕
    ⚠️ 调用方是 `proto/recorder.py` 自己（模块末尾一行）—— **别处不要调**。
    """
    global _sink_record, _sink_write
    _sink_record, _sink_write = record, write


def stamp() -> str:
    """日志时间戳：**年月日 + 毫秒** —— 一轮里同一秒发生好几件事（工具调用 → 返回 → 判定），
    秒级看不出先后；跨天的日志也分不清是哪天。〔沿革 1〕
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


# ──────────────────────────── 事件总线 ────────────────────────────

#: 事件的八种**形状**。每种带什么字段，见各 emitter 与 `proto/recorder.py`。
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
    # 🔴 **记录器是槽位**（不是 import、不是订阅者）—— 没装就什么都不写（那是库的用法）。
    #    ⚠️ 装了之后**处置一个字没改**：单独兜，兜住走 `_mark_hole` 把洞标出来。〔沿革 33〕
    if _sink_record is not None:
        try:
            _sink_record(ev)
        except Exception as e:  # noqa: BLE001 —— 不炸业务，但**必须把洞标出来**（见 _mark_hole）
            _mark_hole(ev, e)
    for slot in list(session.current().subs):
        fn = slot[0]
        try:
            fn(ev)
        except Exception as e:  # noqa: BLE001 —— 见 subscribe 的 docstring
            # ⚠️ **没装记录器时这一行没地方写** —— 那也不能炸业务（同记录器那条的取舍）。
            if _sink_write is not None:
                _sink_write(
                    f"[{stamp()}] ⚠️ 事件订阅者出错"
                    f"（{getattr(fn, '__qualname__', fn)}）：{type(e).__name__}: {e}"
                )


def _mark_hole(ev: dict, e: Exception) -> None:
    """记录器渲染失败 → 在日志里**把洞标出来**（交接三问②：切了必须标）。

    ⚠️ 标洞本身也兜住（比如文件已关）—— 但**不许静默**：正常路径下这行会写进日志，
    读的人一眼知道**丢的是哪一条**，而不是"日志里怎么少了这一段"。
    """
    if _sink_write is None:  # 没装记录器 —— 连"洞"都无处可标（库的用法）
        return
    try:
        _sink_write(
            f"[{stamp()}] ⚠️ **这一条没有落进日志**（记录器渲染失败）："
            f"type={ev.get('type')!r} kind={ev.get('kind')!r}"
            f" —— {type(e).__name__}: {e}"
        )
    except Exception:  # noqa: BLE001 —— 连标洞都写不进去，只能放弃；仍不炸业务
        pass


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


def check_role(role: Any, where: str) -> str:
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
#: ⚠️ 加完还要去 `app/chat.py` **表态**（上屏 / 有专门分支 / 只进日志）——
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
    # stream.stream_agent（`retry` = 这一轮抖动、已重试一次）
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
            "② 在 app/chat.py 表态（上屏 / 有专门分支 / 只进日志）。"
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


def emit(ev: dict) -> None:
    """发一个**已经是最终形状**的事件 —— 给**领域包自己的发射器**用。

    🔴 **为什么要有它**：`decision` 这个形状的发射器住在 `handoff.decision`
    （它要校验 `result_by` ∈ `RESULT_BY` —— 那是**领域词汇**，不是总线该认识的东西）。
    领域包发事件走这一条，总线就不必 import 领域包。〔沿革 33〕

    ⚠️ **校验一样跑**：`type` / `kind` 都在 `_publish` 里查，走这条**不会**绕过它。
    ⚠️ 别处不要用它"绕过"现成的发射器 —— 那些发射器存在的理由是**形状只有一处定义**。
    """
    _publish(ev)


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
    check_role(role, "dump_trace")
    _publish({"type": "dump", "kind": "", "role": role, "tag": tag,
              "messages": list(messages), "ts": stamp()})


# ──────────────── 消息渲染（**各视图共用**：记录器 / 终端 / 台账 / 跑批记录） ────────────────
#
# ⚠️ 原来这儿写着"记录器专用" —— **不是事实**：`app/chat.py`（终端）、`proto/graph.py`
#    （待答透传）、`app/runtime.py`（`_run.md`）都在用。它们是**通用的**
#    "LangChain 消息对象 → 一行文本"，**不认识任何领域词汇** —— 所以留在总线这边，
#    而不是搬去 `proto/recorder.py`：那样**展示层就得反过来依赖记录器**（方向反了）。

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
