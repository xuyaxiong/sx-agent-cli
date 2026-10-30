# 测试技能（来自 sx-agent，本项目口径）

用 `sx-agent/test_data/skills` 里那批"真实复杂度"的技能包，验证原型的多 Skill 执行与交付。
**源头一个字节都不改** —— 改动只发生在 `adapted/`。

```
skills/
├── lesson-prep/SKILL.md          教案（源包含 references/ 3 个，**当前仍是内联的**）
├── guided-learning/SKILL.md      导学单（源包含 references/ 1 个，**当前仍是内联的**）
└── interactive-webpage/SKILL.md  交互网页（单文件）
```

**形状**：每个子目录是一个技能包，入口固定叫 `SKILL.md`，可选 `references/`（见
`proto/skill_pack.py`）。2026-10-30 从"平铺 `*.md`"改成这个形状 —— 那是为了对齐真实技能的形态。

> ⚠️ **下面规则 ④「references 内联」现在可以撤了**：原型已经支持技能包带 `references/`
> （渐进式披露第三层，`read_reference` 按需读）。但**这 3 个适配版还没拆回去** ——
> 内联的内容原样留在 `SKILL.md` 里，等重做适配时再还原成真实的 `references/` 结构。

来源：`sx-agent/test_data/skills`，复制日期 2026-10-29。源头的平台契约与本项目不同，
所以必须有一次**机械适配**；规则固定，见下。

逐字副本**未留档**（2026-10-29 用户确认删除）。要核对改动，去源头目录按下面的指纹比对，
再与 `adapted/` 做 diff：

| 源文件 | md5 |
|---|---|
| `lesson-prep/lesson-prep.md` | `3a3dd0b80b8db63ace56036a7381d415` |
| `lesson-prep/references/教学目标编写指南.md` | `4bb24f89ab6c61d285d7425b956c2fc0` |
| `lesson-prep/references/教学过程设计要点.md` | `1d99c0af30c8ed17491920343e132646` |
| `lesson-prep/references/教案结构模板.md` | `29d0a40ca9178191ce3ca9e5fc949c60` |
| `guided-learning/guided-learning.md` | `79a6a9da5ad761aa6941f845f55905fa` |
| `guided-learning/references/导学单设计指南.md` | `47e8111e1d8364af0c2e9846ca9f63a1` |
| `interactive-webpage/interactive-webpage.md` | `5ea6b32f521d5e1edc9a85a62d0c59cb` |

## 适配规则（只许这六类改动）

| # | 规则 | 说明 |
|---|---|---|
| ① | description 原样取 frontmatter | — |
| ② | 交付通道换名 | `create_artifact` → `write_artifact`；`media_type` / 产物梗概一并删（原型只有 `title` + `content`） |
| ③ | 删掉引用不存在工具的步骤 | `get_current_time` / `query_knowledge` / `fetch_url` / `read_file` / `glob` / `read` / `generate_image` / `generate_audio` / `request_user_input`；连带删掉依赖它们的字段（如"备课时间"） |
| ④ | `references/` 内联为指令附录 | 原型没有参考文件机制；不内联等于把技能掏空 |
| ⑤ | 提问措辞改写 | "必须用 request_user_input 提问，不要写在正文里" → "向用户提问"（**唯一一处改语义**，因为那是别的平台的契约） |
| ⑥ | 去模板占位符 | `{{topic}}` / `{{instruction}}` / `{{attachments}}` 等 → 自然语言（"由派给你的任务给出；没给就问用户，不要自己编"） |

**其余一律不动**：格式模板、工作流程步骤、提问约定（几问、问什么）、语言要求，原样保留 ——
那正是要测的。

## 每个技能改了什么

| 技能 | 改动 |
|---|---|
| `lesson-prep` | 删「第一步 获取时间」「第二步 查参考文件」「第三步 资料收集」；三份 references 内联为附录 A/B/C；`{{grade}}` 等换成自然语言；`create_artifact`→`write_artifact`；删 generate_image 步骤与「备课时间」字段 |
| `guided-learning` | 同上：删时间 / 查参考文件 / 资料收集三步；`导学单设计指南` 内联为附录；删 `{{instruction}}` 与 `{{attachments}}` 段 |
| `interactive-webpage` | 删 `get_current_time` 步骤与页脚时间；`{{主题}}`/`{{受众}}` 换成"由任务给出，没给就先问"；`media_type` 相关表述删除；交付改 `write_artifact` |

## 有意**没有**做的事

- **不把"基于教案"写进 guided-learning / interactive-webpage**。源技能里本来没有这条；
  "基于刚才那份教案"是**用户原话**提的依赖 —— 依赖能不能被桥接正是本轮要测的，
  写进技能就把测试对象改掉了。
- **不删技能里的提问环节**（各 2 问）。它是真实技能的一部分，也是"多轮问答 + 平台投递"的观测点。
- **不加任何参数声明**。原型的 Skill 只有 `name` + `description` + `instructions`，
  技能需要的输入靠自然语言写清楚（源技能的 `input_schema` 不迁移）。

## 怎么被 evals 消费

`adapted/*.md` 是 `name` / `description` 的 frontmatter + 正文即 `instructions`，
与 `proto.skills.Skill` 一一对应。case YAML 用 `skills_dir: evals/skills`
引用（`_child.py` 的加载见 `evals/plan-test-skills.md` §四），加载后按
`skills_mode: replace` 只挂这三个，避免混进原型自带的 `quiz` / `courseware-config`。

## 同步约定

源头更新时**不自动跟随**：重新复制、重做适配、在提交信息里写明跟着哪一版。
适配版是实验输入，不是产品资产。
