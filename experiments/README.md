# experiments —— 推演期的实验与诊断脚本

> 这些脚本**不是 MVP 的一部分**，是推演过程中用来取证的一次性工具。
> 保留它们是为了让 `docs/` 里的实测结论**可复现**。
>
> ✅ **2026-11-06：能跑的都在跑，跑不起来的已删。** 9 个脚本依赖的 API 早被删了
> （判定退休、技能表收进 registry、`skills.py` 拆分…），**一跑就炸**；它们的结论都已写进
> `../docs/`，脚本本身只是取证过程 —— 与其留着"看起来能跑其实不能"，不如删掉
> （`git log` 里都还在）。
>
> ⚠️ 顺带修好 `debug_exit.py`：它**原来也在"跑不起来"那一堆里**（`from proto.skills import
> SKILLS` —— 那张表 2026-11-03 收进 `registry` 了，这行没跟着改），而它验的「出口限制」
> **是活的机制** —— 所以修，不删。

## 怎么跑

从 `langgraph-lab/` 目录，用 `-m`（脚本里是 `from proto.xxx import`，直接 `python experiments/x.py` 会因为 `sys.path` 只有 `experiments/` 而 import 失败）：

```bash
cd langgraph-lab
uv run python -m experiments.debug_exit            # 出口限制（会调真实模型）
uv run python -m experiments.probe_api             # API 签名核查（不调模型）
```

> ⚠️ **没有 fake 模式了。** 曾经有个 `PROTO_FAKE_MODEL=1`，2026-10-28 拆掉 ——
> 它只假了执行器与判定（主 agent 仍调真实 API），而且**短路了 `run_task` 的整个轮次循环**，
> 所以拿它测多轮行为、出口限制、判定视野全都测不到，反而给出"跑通了"的假信号。
>
> `exp01`–`exp07` 里的 `FakeChatModel` 是**另一回事**：那些实验要测的是
> **LangGraph 的机制**（interrupt 能不能穿透、为什么被吞），不是模型行为 ——
> 用假模型是为了**确定性可复现**，用真模型反而测不准。

## 能跑的（文档引用它们作为证据）

| 脚本 | 验什么 | 文档引用 |
|---|---|---|
| **`debug_exit.py`** | 出口限制：执行器既没完成、也没提问时，平台会不会把它推回去 | `solution.md` §7.1 · `docs/README.md`「跑起来」 |
| `probe_api.py` | 已装版本里离线文档提到的关键 API 是否可用、签名是否一致 | `solution.md` §7.1 |
| `exp01`–`exp07` | LangGraph `interrupt()` 机制取证（**已否决路线的证据**）：能否穿透 tool 包装 / 为什么被吞 / 批量恢复 / 上下文隔离量化 / 答复能否传回正确的执行器 | `docs/discussion-log.md`「三、推演过程」§6.11 §9.5 |

## 其它

- （原先这里有个 `main.py`：`uv init` 的脚手架桩，无引用，2026-10-29 已删。）
- 这些脚本的结论**都已写进 `../docs/`**，脚本本身只是取证过程。
