# experiments —— 推演期的实验与诊断脚本

> 这些脚本**不是 MVP 的一部分**，是推演过程中用来取证的一次性工具。
> 保留它们是为了让 `docs/` 里的实测结论**可复现**。
>
> ⚠️ **5 个已经跑不起来了**（见下表），因为原型重构时删掉了它们依赖的 API。
> 保留原样是为了留下"当时验的是什么"的记录，**不打算修**。

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

## 跑不起来的（依赖的 API 已被删）

| 脚本 | 当时验什么 | 断在哪 |
|---|---|---|
| `debug_abandon.py` | 挂起后用户**不回答、直接换话题** | L48 `build("interrupt")`，而 `proto/graph.py` 的 `build()` **不收参数** → 运行时 TypeError |
| `debug_body_question.py` | 执行器**用正文提问**时系统能不能接住 | `proto.skills.Deliverable`、`run_skill` 已删 |
| `debug_criteria.py` | `completion_criteria`（评审人 D1 的落点，**结论是砍掉**） | `proto.agent._check_criteria`、`_judge_completion` 已删 |
| `debug_delivery.py` | 交付物清单（一个 skill 可以有任意多样、形态不同） | `proto.skills.run_skill` 已删 |
| `debug_executor.py` | 执行器行为 | `proto.skills.finish`、`run_skill` 已删 |
| `test_judge_material.py` | **判定**的「实际执行结果」怎么取材（保头保尾 / 全部轮次） | `proto.handoff.judgement` **2026-11-05 整个模块删了**（判定退休）|
| `debug_judge_variants.py` | 4 种「完成判定」提示词对比（**F 6/6 vs C 4/6** 那条结论就是它跑出来的）| 同上 —— 判定提示词已经不存在 |
| `debug_clip_marker.py` | 截断标记的措辞（判定会把它当成作者占位符）| 同上 —— 判定那套截断没了 |

## 其它

- （原先这里有个 `main.py`：`uv init` 的脚手架桩，无引用，2026-10-29 已删。）
- 这些脚本的结论**都已写进 `../docs/`**，脚本本身只是取证过程。
