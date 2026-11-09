"""单元级：**冻结后（打包成 exe）的目录约定**（不调 LLM、不打 exe）。

    uv run python -m evals.unit_frozen_paths

**为什么要有它**：打包后才走的那条分支，**平时没有任何人跑** ——
而它已经出过两次 bug，两次都是"开发态永远碰不到、打包才发现"：

| bug | 现象 |
|---|---|
| 示例技能铺错 | 把 `README.md` 也当技能铺进用户的 `skills/` |
| trace 附录写错地方 | `evals/run.py` 自己算 `HERE / "runs"`，冻结后是 `_internal/evals/runs/` —— **用户永远看不到** |

而**跑一次打包要几分钟、用户还得干等**（用户 2026-10-30：*"你不要频繁的打包
每搞一次我都要等半天 **你先用脚本的方式测**"*）。所以这里用
`sys.frozen = True` + 假 exe 目录**把那条分支完整走一遍**，秒级出结果。

> 🔴 **真打包仍然不可替代**（它验的是"冻结本身"：漏收依赖、`_MEIPASS`、双击启动）。
> 但"路径算得对不对"这类**纯逻辑**，这里就能验完，不用麻烦用户等。
"""

# ruff: noqa: T201  # 诊断输出即本脚本的产出物

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
import os

# 🔴 **单测的日志打 tag**（2026-11-06）：`logs/` 里真会话与测试垃圾混在一起，复盘时
#    grep 不动 —— 打上 tag 后文件名是 `proto-<ts>-unit-<名字>.log`，一眼可辨
#    （清理：`rm logs/*-unit-*.log`）。复用跑批那个 `PROTO_LOG_TAG`（`evals/_child.py`）。
# ⚠️ **必须排在所有非标准库 import 之前**：`proto.trace` 在 import 那一刻读 env 定文件名，
#    而 `from app import chat` 这种**间接** import 也会把它拉进来（实测踩过：
#    第一版插在 `from app import chat` 之后 → 日志照样没 tag）。
#    守卫：`unit_feed_waiting` ⑮。
os.environ["PROTO_LOG_TAG"] = "unit-frozen-paths"
# ⚠️ **故意不设 `PROTO_LOG_DIR`** —— 这个模块测的就是**默认路径**（冻结后落在 exe 旁边），
#    设了就被顶掉了。它自己写进 `fake/logs`（临时目录，随测试删），不污染 `logs/`。
#    守卫 `unit_feed_waiting` ⑮ 对它**豁免**（见那边的 `_NO_LOG_DIR_EXEMPT`）。

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n      {detail}" if detail else ""))


def _same(a: Path, b: Path) -> bool:
    """⚠️ 两边都要 `resolve()` 再比 —— Windows 上 `XUYAXI~1` 和 `xuyaxiong` 是同一个目录，
    直接比字符串会**假红**（本脚本第一版就踩了）。"""
    return a.resolve() == b.resolve()


def main() -> None:
    fake = Path(tempfile.mkdtemp(prefix="frozen-"))
    (fake / "sxagent.exe").write_text("")

    # 造出"冻结后"的三个标记：frozen / exe 路径 / 解包目录（= --add-data 的落点）
    sys.frozen = True  # type: ignore[attr-defined]
    sys.executable = str(fake / "sxagent.exe")
    sys._MEIPASS = str(REPO)  # type: ignore[attr-defined]

    # ⚠️ 必须在设完标记**之后**导入 —— 这些模块在 import 时就把路径算好了
    import proto.config as cfg
    import proto.recorder as recorder
    import proto.trace as trace
    from app import runtime
    from evals import report, run

    print(f"假 exe 目录：{fake}\n")

    print("① 所有路径都落在 **exe 旁边**（不是 `_internal/`、不是仓库里）")
    check("app_root()", _same(cfg.app_root(), fake), str(cfg.app_root()))
    check("runtime.ROOT", _same(runtime.ROOT, fake))
    check("技能目录", _same(runtime.DEFAULT_SKILLS_DIR, fake / "skills"))
    check("运行记录", _same(runtime.RUNS_DIR, fake / "runs"))
    check("recorder 日志", _same(recorder.LOG_DIR, fake / "logs"))
    # 🔴 这两个曾经自己算 `HERE / "runs"` —— 冻结后指到 `_internal/` 里去
    check("run.RUNS_DIR 走同一来源", _same(run.RUNS_DIR, fake / "runs"))
    check("report.RUNS_DIR 走同一来源", _same(report.RUNS_DIR, fake / "runs"))

    print("\n② 只读资源取 `_MEIPASS`（随包带进来的示例技能）")
    check("SEED_SKILLS_DIR", _same(runtime.SEED_SKILLS_DIR, REPO / "skills"))

    print("\n③ 首次运行初始化")
    runtime.ensure_app_dirs()
    # 技能是**包**（子目录 + SKILL.md），不是散装 .md —— 见 `proto/skill_pack.py`。
    # ⚠️ 断言**规则**（源目录里有几个包就铺几个），**不写死数字** ——
    # 写死的话每加一个技能这条就假红（2026-10-30 踩过）。
    src_packs = sorted(
        p.name for p in (REPO / "skills").iterdir() if (p / "SKILL.md").is_file()
    )
    seeded = sorted(p.name for p in (fake / "skills").iterdir() if (p / "SKILL.md").is_file())
    check("铺的 == 源目录里的技能包（散文件如 README.md 不铺）", seeded == src_packs, str(seeded))
    check(
        "包里的 SKILL.md 真在",
        bool(seeded) and all((fake / "skills" / n / "SKILL.md").is_file() for n in seeded),
    )
    # 带子目录的包（`references/standards/`）要**整棵树**拷过去
    check(
        "子目录也一起拷了",
        all(
            not any((REPO / "skills" / n).rglob("*/"))
            or any((fake / "skills" / n).rglob("*/*"))
            for n in seeded
        ),
    )
    check("幂等（再跑一次什么都不做）", runtime.ensure_app_dirs() == [])

    print("\n③′ `build_exe.py` 指的入口**真在**")
    # 🔴 2026-11-09 实测踩到：`chat.py` 从 `evals/` 搬到 `app/` 之后，
    #    `build_exe.ENTRY` 还是 `ROOT / "evals" / "chat.py"` —— **一个字都没报错**，
    #    直到真去打包才炸（`ERROR: Script file … does not exist`）。
    #    ⇒ 入口路径**是接线，不是机制**：这里当场盯着，别等打包那几分钟。
    #    有效性自检：把 ENTRY 改回 `evals` → 这里必须红。
    import build_exe
    check("ENTRY 指向的文件存在", build_exe.ENTRY.is_file(), str(build_exe.ENTRY))
    check("入口那个包有 `__init__.py`（PyInstaller 才当包解析）",
          (build_exe.ENTRY.parent / "__init__.py").is_file())

    print("\n④ trace 附录落进**用户看得到**的那份 `_run.md`")
    tag = "unit-frozen-paths"
    out = run.RUNS_DIR / tag / "artifacts" / f"{tag}__r1"
    out.mkdir(parents=True, exist_ok=True)
    (out / "_run.md").write_text("# 假装是逐轮记录\n", encoding="utf-8")
    logf = fake / "logs" / "proto-unit.log"
    logf.write_text("[2026-10-30 00:00:00.000] 主 agent 本轮 消息轨迹（1 条）:\n", encoding="utf-8")
    runtime.append_trace({"trace_log": str(logf), "case": tag, "rep": 1}, tag)
    check("附录写进去了", "附录 · 原始 trace" in (out / "_run.md").read_text(encoding="utf-8"))
    check("没往 `_internal/` 里写", not (fake / "_internal").exists())

    shutil.rmtree(fake, ignore_errors=True)
    print("\n" + "═" * 78)
    print(f"{'全部通过' if not FAIL else '有失败'}：{len(PASS)}/{len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ❌ {name}")
    print("═" * 78)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
