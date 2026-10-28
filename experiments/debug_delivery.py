"""诊断：交付物清单（一个 skill 可以有任意多样、形态不同）。

  artifact — 落产物库（教案 / 导学单 / 课件）
  inline   — 走回复正文（业务侧要解析的协议块、边界标记、配置），**不落库**

关键：**每样交付物独立判定，「证据」随形态而变**。
若把 inline 交付物按 artifact 判，那个 skill 会**永远失败**——它压根不会去写产物库。

    uv run python debug_delivery.py
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from proto import trace
from proto.config import describe
from proto.skills import ARTIFACTS, SKILLS, run_skill

PARAMS = {"年级": "三年级", "学科": "数学", "课题": "分数的初步认识"}

CASES = [
    ("单样 · 落库", "lesson-prep"),
    ("单样 · 正文", "courseware-config"),
    ("多样 · 落库+正文", "lesson-with-config"),
]


def run(label: str, key: str) -> bool:
    skill = SKILLS[key]
    before = set(ARTIFACTS)
    trace.section(f"=== {label}（{key}）===")
    print(f"\n── {label} · {key} ──")
    print(f"   声明的交付物：{[(d.name, d.kind, '必交' if d.required else '可选') for d in skill.deliverables]}")

    r = run_skill(skill, PARAMS)
    new = [k for k in ARTIFACTS if k not in before]

    print(f"   status           = {r.status}")
    print(f"   落库新产物        = {new or '（无）'}")
    for d in r.deliverables:
        if d.kind == "artifact":
            print(f"   交付物「{d.name}」= artifact {d.artifact_id}")
        else:
            head = d.content[:60].replace("\n", " ")
            print(f"   交付物「{d.name}」= inline {head!r}…")
    if r.missing_deliverables:
        print(f"   ⚠️ 缺失            = {r.missing_deliverables}")

    # 逐样核对：声明的每样都拿到了吗？形态对得上吗？
    got = {d.name: d for d in r.deliverables}
    ok = r.status == "done" and all(d.name in got for d in skill.deliverables)
    if ok:
        for d in skill.deliverables:
            g = got[d.name]
            if d.kind == "artifact" and not g.artifact_id:
                ok = False
            if d.kind == "inline" and not g.content:
                ok = False
    detail = []
    for d in skill.deliverables:
        g = got.get(d.name)
        if not g:
            detail.append(f"{d.name}❌")
        elif d.kind == "artifact":
            detail.append(f"{d.name}→{g.artifact_id}")
        else:
            detail.append(f"{d.name}→正文{len(g.content)}字")
    print(f"   → {'✅' if ok else '❌'} {' / '.join(detail)}")
    return ok


if __name__ == "__main__":
    print(f"模型：{describe()}")
    print(f"日志：{trace.LOG_FILE}")

    results = [run(label, key) for label, key in CASES]

    trace.close()
    print(f"\n通过：{sum(results)}/{len(results)}")
    print(f"完整日志：{trace.LOG_FILE}")
