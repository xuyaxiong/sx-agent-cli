# langgraph-lab

一次架构推演的**完整工作区**：从用户提出痛点，到机制核查、原型验证、外部评审、逐条收口。

针对的是「**多 Skill + Subagent**」这个场景 —— 具体说，是给 `sx-agent` 找一条
**既保留上下文隔离、又不重复问用户**的路。

---

## 一句话结论

> **Skill 是专业能力，Task 是具体业务工作，Sub Agent 是可选执行者 —— 三者正交。**
>
> P1/P2/P3 不是"隔离"的代价，是**「把 subagent 边界画在整个 skill 生命周期上」**的代价。
> 把边界收窄到「**参数齐了之后的执行段**」，三个问题结构性消失，**而隔离保留**。

---

## 目录

```
langgraph-lab/
├── docs/          文档 —— 从哪份看起见 docs/README.md
├── proto/         最小原型（可运行）—— 技能 / 技能加载 / 产物 三个模块分得清（见 docs/README.md）
├── evals/         跑批回归 + 交互式入口（见其 README）
├── experiments/   推演期的实验与诊断脚本（其中 5 个已跑不起来，见其 README）
├── build_exe.py   打包成能发给别人的 exe（见下「打包」）
├── logs/          每次运行的完整日志（含 LLM 原文）—— 一次性的，已 gitignore
├── dist/ build/   打包产物 —— 已 gitignore
├── pyproject.toml / uv.lock / .python-version
└── .env / .env.example
```

### 先看哪份文档

| 我想…… | 看 |
|---|---|
| **了解要落地什么** | **`docs/solution.md`** |
| **改设计前，确认需求没被牺牲** | **`docs/solution.md` §0.6 需求清单** |
| 调提示词 | `docs/solution.md` §十一 |
| 讲清楚架构（给人看） | `docs/architecture-diagram.md` + `docs/figures/*.svg` |
| 每次架构迭代后跑回归 | `docs/scenarios.md` |
| 查某个评审点怎么定的 | `docs/architecture-review.md` |
| 查来龙去脉 | `docs/discussion-log.md` 工作日志 |

完整地图见 **`docs/README.md`**。

---

## 跑起来

```bash
# 1) 配模型
cp .env.example .env      # 填好 API key 与模型名

# 2) 交互式
uv run python -m proto

# 3) 单轮
uv run python -m proto --say "帮我备课，三年级数学"

# 4) 跑验证脚本（在 experiments/，见其 README）
uv run python -m experiments.debug_exit
```

> ⚠️ **没有 fake / 离线模式** —— 所有 LLM 调用都走真实模型。
> 曾经有个 `PROTO_FAKE_MODEL=1`，2026-10-28 拆掉了：它只假一半（主 agent 仍调真实 API），
> 而且短路了 `run_task` 的整个轮次循环，测多轮行为时给的是**假信号**。

> ⚠️ **Windows 上要设 `PYTHONUTF8=1`**，否则中文输出乱码。
> 脚本自己会 `reconfigure(stdout)`，但直接 `python -c` 打印中文时仍会撞 GBK。

---

## 打包发给别人（没装 Python 的人也能跑）

```bash
uv run python build_exe.py          # → dist/sxagent/
```

打出来是**单目录**（不是单文件），用户拿到手长这样：

```
sxagent/
├─ sxagent.exe        ← 双击就跑（弹一个控制台窗口，不用敲命令）
├─ .env              ← **你填好再发**（key / 模型名 / 端点）；随包只放 .env.example
├─ skills/           ← 首次运行自动建，并放 3 个示例技能；用户往里丢 .md 就加技能
├─ runs/             ← 首次运行自动建；**每次对话一份记录，让他们整包发回来**
└─ _internal/        ← 依赖文件，用户不用碰
```

> 🔴 **发出去之前必须验收**：在一台**没装 Python** 的机器上跑通一次完整对话
> （技能列出来 → 派任务 → 出产物 → `runs/` 有记录）。
> PyInstaller **漏收依赖是常态**，langchain / langgraph 这种动态导入重灾区尤其是 ——
> 本次就靠这一步抓出两个真 bug（见 `docs/discussion-log.md` F19）。
>
> ⚠️ 告诉用户**整个文件夹拷走，别只拷 exe**。

---

## 环境

| | |
|---|---|
| Python | 3.12 |
| langgraph | 1.2.18 |
| langchain | 1.4.7 |
| langchain-core | 1.7.0 |
| 实测模型 | DeepSeek v4 pro（DashScope 兼容端点）· 豆包 doubao-seed-2.1-turbo（Ark） |

⚠️ **换模型必须重新验证** —— 实测豆包在结构化输出上会**静默失效**（返回空消息、不报错）。

---

## 三条使用规矩

**① 改设计前先看需求清单**（`docs/solution.md` §0.6）

> 某条需求如果被某个设计牺牲了，**必须显式说明，不能悄悄丢**。

**② 每次架构迭代后跑一遍回归集**（`docs/scenarios.md`）

> 架构迭代最容易"**修好一个、弄坏一个**"。
> 本次实测：去掉 `load_skill` 修好 S12，却立刻暴露 S13、S14。

**③ 结论必须实测，优先真实模型**

> 这半天里几乎每一条"想当然"都被实测推翻过。

---

## 与原项目的关系

本仓库是**独立的验证工作区**，不改动 `sx-agent`（只读核查）。
落地建议见 `docs/solution.md` §八。
