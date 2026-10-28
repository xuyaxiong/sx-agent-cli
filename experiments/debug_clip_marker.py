"""隔离复现：**平台的截断标记，被判定当成了作者的占位符**（F1）。

## 现象（`evals/runs/validate/s13-isolation-long-artifact__r1`）

一个 11,953 字、内容完整的教案，被判成 `incomplete`。判定的 `missing` 原文：

> 产物中教案主体内容被省略了（"**此处省略 11153 字**"），实际并未提供完整的教学过程…

可那句"此处省略 11153 字"**是平台自己写的** —— `_clip()` 为了控制判定提示词的体积，
把产物截到 800 字，中间插了这行标记。判定规则里又写着
"占位内容、待补充项、半成品（如「待补充」「TODO」）**不能视为完成**"。

于是：**产物越长，越必然被判成半成品。**

## 修法（已落到 `executor.py`）

两处，**分开测**，才知道哪一处真的有用：

| | 改了什么 |
|---|---|
| **只改标记** | `……（此处省略 N 字）……` → `……【平台截断：…不是作者写的占位符，也不代表内容缺失】……` |
| **标记 + 说明** | 再在 `_JUDGE_RULES` 里加一段，明说平台会截断、看到标记不要判未完成 |

## 四个变体

| 变体 | 判定看到的东西 |
|---|---|
| **A · 修复前** | 旧标记 + 旧规则（复刻原状） |
| **B · 只改标记** | 新标记 + 旧规则 |
| **C · 标记+说明**（现状） | 新标记 + 新规则 |
| **D · 不截断** | 全文（上界：判定能看全时会怎样） |

**另有一组反例做对照**（`PLACEHOLDER`：内容里**真有**作者写的"待补充"）——
没有这组，B/C 变好就可能只是"把判定弄瞎了"，而不是"修好了"。

    uv run python -m experiments.debug_clip_marker
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from proto import trace
from proto.config import describe
from proto.executor import _JUDGE_RULES, _judge, _judge_material
from proto.skills import ARTIFACTS, Skill

# ──────────────────────────── 被测的技能 ────────────────────────────

SKILL = Skill(
    name="full-lesson-plan",
    description="完整教案",
    instructions=(
        "产出一份**完整详细**的教案，写入产物库。要求：\n"
        "- **1000 字以上**，不要精简、不要概括、不要写「略」、不要用省略号跳过；\n"
        "- 必须包含：教学目标、重难点分析、学情分析、教学过程、板书设计、作业设计、教学反思；\n"
        "- 教学过程要分环节，写清教师活动、学生活动、设计意图。"
    ),
)
GIVEN = "三年级数学《分数的初步认识》第一课时，人教版，第一课时为「认识几分之一」。"

# ──────────────────────────── 完整的长产物（真的一段，没截断） ────────────────────────────

COMPLETE = """# 《分数的初步认识——认识几分之一》教学设计

## 一、教材分析

本课是人教版三年级上册第八单元《分数的初步认识》的第一课时。分数是小学数学中
第一次出现的"比率"概念，它与整数在意义、读写、比较上都有本质差别。教材从"分东西"
的生活情境切入：两个人分月饼，每人分得半个——半个怎么写？由此产生认知冲突，
引出"二分之一"这个新的数。这一课的核心不是计算，而是**建立"把一个整体平均分成若干份，
其中的一份就是几分之一"这一模型**。

教材编排的三个层次很清楚：先认识二分之一（分月饼），再类推认识四分之一（折纸），
最后概括"几分之一"的一般意义并比较大小。教学中要紧扣"平均分"这个前提——
不平均分，就不能用分数表示。

## 二、学情分析

**知识基础**：学生已经熟练掌握整数、平均分、除法的含义，能用"每份同样多"描述平均分。

**生活经验**：学生日常有分蛋糕、分水果的经历，对"一半"有直觉，但**没有"半个"的数学符号经验**，
容易把"二分之一"读成"一分之二"或写成"2/1"。

**认知特点**：三年级学生以具体形象思维为主，需要借助折、涂、画等操作建立表象。

**困难预估**：一是**忽略"平均分"前提**，认为任意分成两份都能用 1/2 表示；
二是**把分数的大小与分母大小正向对应**，认为 1/4 > 1/2；三是书写时分子分母位置颠倒。

## 三、教学目标

1. **知识技能**：结合具体情境初步认识几分之一，会读、会写几分之一，知道分数各部分的名称。
2. **过程方法**：经历"分一分、折一折、涂一涂"的操作过程，在动手与表达中抽象出分数的意义。
3. **情感态度**：体会分数来源于生活需要，感受数学与生活的联系，在操作中体验成功的乐趣。

## 四、教学重难点

**重点**：认识几分之一，理解"平均分"是分数的前提。

**难点**：理解几分之一的含义，特别是"把谁平均分""分成几份""表示其中的几份"这三层意思。

**突破策略**：用"三次对比"推进——平均分与不平均分对比（建立前提），
不同形状的 1/2 对比（抽象出本质），1/2 与 1/4 对比（辨析大小）。

## 五、教学准备

教师：月饼图卡、圆形与长方形纸片若干、多媒体课件、磁性黑板贴。
学生：每人 3 张同样大小的圆片、1 张长方形纸、彩笔。

## 六、教学过程

### 环节一：情境导入，制造冲突（5 分钟）

**教师活动**：出示两个小朋友分月饼的图。"老师这里有 4 块月饼，分给 2 个人，怎么分最公平？"
（学生：每人 2 块。）"现在只有 1 块月饼，还要分给 2 个人，怎么分？"

**学生活动**：说出"一人一半"。

**教师追问**："一半"是多少块？能用我们学过的数表示吗？

**学生活动**：发现整数不够用了——半个没法用 1、2、3 表示。

**设计意图**：从整数不够用出发，让学生自己感受到"需要一个新的数"，而不是老师直接告知。

### 环节二：认识二分之一（12 分钟）

**教师活动**：把月饼图沿中线剪开，两半重合。"看，两半完全重合，说明怎么分？"（平均分。）
"把一块月饼平均分成 2 份，每份是这块月饼的一半，也就是它的二分之一，写作 1/2。"

**教师示范书写**：先写横线（表示平均分），再写下面的 2（表示平均分成 2 份），
最后写上面的 1（表示取其中的 1 份）。**边写边说三句话**。

**学生活动**：伸出手指跟写两遍，同桌互相说"我把（什么）平均分成（几）份，
每份是它的（几分之一）"。

**教师活动**：发圆片。"请把圆片平均分成 2 份，涂出它的 1/2。"
巡视时收集两类作品：平均分的与不平均分的。

**辨析**：把两类作品都贴到黑板上。"这两个涂色部分都能用 1/2 表示吗？"
引导学生说出：**不平均分就不能用分数表示**。

**设计意图**：用"错误资源"正面撞击"平均分"这个前提，比老师反复强调有效。

### 环节三：折纸活动，认识四分之一（10 分钟）

**教师活动**："把同样一张圆片平均分成 4 份，每份是它的几分之几？"

**学生活动**：动手折、涂，得出 1/4。

**教师追问**："同样是 1/4，为什么涂色的形状不一样，却都叫 1/4？"
（因为都是把同一张圆片平均分成 4 份，取了其中的 1 份。）

**设计意图**：让学生从"形状"中抽象出"份数关系"，这是分数概念的关键一跃。

### 环节四：对比辨析，突破难点（8 分钟）

**教师活动**：出示两个同样大的圆，一个涂 1/2，一个涂 1/4。"哪个涂色部分大？"

**学生活动**：多数学生凭直觉能说出 1/2 大，但**追问"为什么"时说不清**。

**教师活动**：把两个圆重叠比一比。"同样大的圆，平均分的份数越多，每一份就越小。"

**设计意图**：把"分母越大分数越小"从机械记忆变成可看见的事实。

### 环节五：巩固练习（8 分钟）

**教师活动**：出示三组图：①平均分成 3 份涂 1 份；②不平均分成 3 份涂 1 份；
③平均分成 3 份涂 2 份。问"哪些能用 1/3 表示？"

**学生活动**：判断并说明理由。②不能用（不是平均分），③不能用（取的是 2 份不是 1 份）。

**教师活动**：出示判断题"把一个圆分成 3 份，每份是它的 1/3"，请学生用手势判断。

**设计意图**：正例与反例同时出现，逼学生同时守住"平均分"和"取一份"两个条件。

### 环节六：课堂小结（2 分钟）

**教师活动**："这节课我们认识了什么数？它和以前学的数有什么不一样？"
"要表示几分之一，必须先做什么？"

**学生活动**：回顾"平均分"是前提，说出分数的读写方法。

**设计意图**：把操作经验提炼成一句话——**先平均分，再取一份**。

## 七、板书设计

```
        分数的初步认识——认识几分之一

  把一块月饼平均分成 2 份，每份是它的二分之一，写作 1/2
                                        ↑ 横线：平均分
                                     2 → 平均分成 2 份
                                   1 → 取其中的 1 份

  前提：必须"平均分"          发现：分的份数越多，每份越小
```

## 八、作业设计

**基础题（必做）**：
1. 把一张长方形纸平均分成 4 份，涂出它的 1/4，并写一写你是怎么分的。
2. 判断：下面哪些图形的涂色部分能用 1/2 表示？为什么？

**拓展题（选做）**：
3. 用同样大的正方形纸，分别折出 1/2、1/4、1/8，比较它们的大小，你发现了什么？

**设计意图**：基础题守住"平均分"这一前提；拓展题把"份数越多每份越小"延伸到 1/8，
为下一课时比较大小做铺垫。

## 九、教学反思（预设）

本课的关键不在"会不会写 1/2"，而在"能不能守住平均分这个前提"。
预计学生在判断题环节仍会出现"分成 3 份就是 1/3"的错误，
需要在反馈时反复回到"平均分"三个字上。折纸活动要留足时间，
让每个学生都经历"分—涂—说"的完整过程，避免动手被压缩成走过场。
"""

#: 反例：**真有**作者写的占位符（内容确实不完整）
PLACEHOLDER = COMPLETE.replace(
    "### 环节三：折纸活动，认识四分之一（10 分钟）",
    "### 环节三：折纸活动，认识四分之一（10 分钟）\n\n（此处省略 1200 字，具体活动步骤待补充）\n",
).replace(
    "## 七、板书设计",
    "## 七、板书设计\n\n（待补充）\n",
)


# ──────────────────────────── 变体 ────────────────────────────

#: 修复前的规则 = 现在的规则**去掉**新加的那段说明（按小标题切，免得两处文本不同步）
_OLD_RULES = _JUDGE_RULES.split("⚠️ **关于「平台截断」")[0].rstrip()

#: 修复前的标记
_OLD_MARK = "……（此处省略 {n} 字）……"


def _clip_old(text: str, budget: int = 800) -> str:
    """复刻修复前的 `_clip` —— 只换标记措辞，截断逻辑一模一样。"""
    if len(text) <= budget:
        return text
    head = int(budget * 0.6)
    tail = budget - head
    return f"{text[:head]}\n" + _OLD_MARK.format(n=len(text) - budget) + f"\n{text[-tail:]}"


VARIANTS = {
    "A 修复前（旧标记+旧规则）": ("old", "old"),
    "B 只改标记": ("new", "old"),
    "C 标记+说明（现状）": ("new", "new"),
    "D 不截断（上界）": ("none", "new"),
}


def build_material(text: str, mark: str) -> str:
    if mark == "none":
        return f"【产物 art_001】\n{text}"
    if mark == "old":
        return f"【产物 art_001】\n{_clip_old(text)}"
    ARTIFACTS["art_001"] = {"title": "完整教案", "content": text}
    return _judge_material(["art_001"], {"messages": []})  # 走真实代码路径


# ──────────────────────────── 跑 ────────────────────────────


def dump_materials() -> None:
    """把「判定实际看到的东西」落盘 —— 光看结论分不清"判对了"还是"蒙对了"。

    产物原文也一并落盘，人可以自己比对：**判定的输入里到底有没有那条缺陷**。
    """
    from pathlib import Path

    d = Path(__file__).resolve().parent.parent / "logs" / "clip-marker-materials"
    d.mkdir(parents=True, exist_ok=True)
    (d / "完整产物_原文.md").write_text(COMPLETE, encoding="utf-8")
    (d / "真占位符_原文.md").write_text(PLACEHOLDER, encoding="utf-8")
    for vname, (mark, _) in VARIANTS.items():
        for cname, text in (("完整", COMPLETE), ("真占位符", PLACEHOLDER)):
            (d / f"{cname}__{_slug(vname)}.txt").write_text(
                build_material(text, mark), encoding="utf-8"
            )
    print(f"判定输入已落盘（可人工比对）：{d}\n")


def _slug(s: str) -> str:
    import re

    return re.sub(r"[^\w一-鿿]+", "-", s).strip("-")[:50]


def main() -> None:
    import proto.executor as ex

    reps = 3
    print(f"模型：{describe()}　每格 {reps} 次\n")
    dump_materials()
    print(f"完整产物 {len(COMPLETE)} 字｜反例（含真占位符）{len(PLACEHOLDER)} 字")
    print(f"判定预算 JUDGE_ITEM_BUDGET = {ex.JUDGE_ITEM_BUDGET} 字\n")

    results: dict[tuple[str, str], list[str]] = {}
    for case_name, text, expect in (
        ("完整", COMPLETE, "completed"),
        ("真占位符", PLACEHOLDER, "incomplete"),
    ):
        print(f"\n{'═' * 72}\n【{case_name}产物】期望 = {expect}\n{'═' * 72}")
        for vname, (mark, rules) in VARIANTS.items():
            got: list[str] = []
            ex._JUDGE_RULES = _OLD_RULES if rules == "old" else _JUDGE_RULES
            for _ in range(reps):
                j = _judge(SKILL, ["art_001"], build_material(text, mark), GIVEN)
                got.append(j.status)
            ex._JUDGE_RULES = _JUDGE_RULES
            results[(case_name, vname)] = got
            hit = sum(1 for g in got if g == expect)
            mark_ = "✅" if hit == reps else ("⚠️" if hit else "🔴")
            print(f"  {mark_} {vname:<26} 判对 {hit}/{reps}　{got}")

    print(f"\n{'═' * 72}\n汇总\n{'═' * 72}")
    print(f"{'变体':<28}{'完整→completed':<20}{'真占位符→incomplete'}")
    for vname in VARIANTS:
        a = results[("完整", vname)]
        b = results[("真占位符", vname)]
        ra = f"{sum(1 for x in a if x == 'completed')}/{len(a)}"
        rb = f"{sum(1 for x in b if x == 'incomplete')}/{len(b)}"
        print(f"{vname:<28}{ra:<20}{rb}")
    print()
    print("看两列：**第一列要高（别误伤），第二列也不能塌（别把判定弄瞎）**。")

    # ── 预算扫描：答案会不会只是"把预算调大"？──
    #
    # 🔴 **踩过的坑**：`_clip(text, budget: int = JUDGE_ITEM_BUDGET)` 的默认参数在
    # **函数定义时**就绑定了 —— 运行时改 `ex.JUDGE_ITEM_BUDGET` **不生效**，
    # 第一版扫描四行其实全跑的 800（还跟变体 D 的结果自相矛盾，才发现）。
    # 所以这里把 `_clip` 换成一个**每次调用都读当前值**的版本。
    orig_clip = ex._clip

    def clip_live(text: str) -> str:
        b = ex.JUDGE_ITEM_BUDGET
        if len(text) <= b:
            return text
        head = int(b * 0.6)
        return f"{text[:head]}\n" + ex._CLIP_MARK.format(n=len(text) - b) + f"\n{text[-(b - head):]}"

    ex._clip = clip_live
    print(f"\n{'═' * 72}\n预算扫描（标记用修复后的；产物 {len(COMPLETE)} 字）\n{'═' * 72}")
    print(f"{'JUDGE_ITEM_BUDGET':<22}{'截断？':<10}{'完整→completed':<20}{'真占位符→incomplete'}")
    for budget in (800, 1600, 2400, 4000):
        ex.JUDGE_ITEM_BUDGET = budget
        row = []
        for text, expect in ((COMPLETE, "completed"), (PLACEHOLDER, "incomplete")):
            got = [_judge(SKILL, ["art_001"], build_material(text, "new"), GIVEN).status
                   for _ in range(reps)]
            row.append(f"{sum(1 for x in got if x == expect)}/{len(got)}")
        clipped = "否" if budget >= len(COMPLETE) else "是"
        print(f"{budget:<22}{clipped:<10}{row[0]:<20}{row[1]}")
    ex._clip = orig_clip
    ex.JUDGE_ITEM_BUDGET = 800
    print()
    print("如果预算一调大两列就都对了 —— 那真正的修法是**调预算**，不是改措辞。")
    trace.close()


if __name__ == "__main__":
    main()
