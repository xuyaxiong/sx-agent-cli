"""打一个能直接发给别人的 Windows 应用（**单目录**）。

    uv run python build_exe.py          # → dist/sxagent/sxagent.exe

## 为什么是 onedir 不是 onefile

onefile 每次启动都要把上百 MB 解压到临时目录，**启动慢好几秒**；
而我们本来就要给用户一个目录（`.env` / `skills/` / `runs/` 都在 exe 旁边）。
代价是目录里多一个 `_internal/`（依赖文件，用户不用碰）——
所以**要提醒用户整个文件夹拷走，别只拷 exe**。

## 🔴 打出来必须真跑一遍

PyInstaller **漏收依赖（under-collection）是常态**，langchain / langgraph 这种
动态导入重灾区尤其是。下面那串 `--collect-*` 就是为它加的，但**不能假定够了**：
必须在一台**没装 Python** 的机器上跑通一次完整对话才算数（验收步骤见 `evals/README.md`）。

## 🔴 重建**不许**把用户数据删掉

`--noconfirm` 会把目标目录**整个删掉**再重建 —— 而那个目录里除了构建产物，还住着
**用户的数据**：填好的 `.env`（真 key）、自己加的 `skills/`、每次对话的 `runs/`。
（2026-11-02 实测踩到：打完 `.env` 没了，只能手动救回来。）

所以打包前先把 `KEEP` 里那几样挪到 `<dist>/_keep-<时间>`，打完**原样放回**；
打失败也放（`finally`，含"挪到一半就炸"）—— 一次打包失败不该让用户的 key 留在暂存目录里。
**挪用的是同卷改名（`os.rename`），不是 `shutil.move`** —— 后者改名失败会回退成
"先复制、再删源"，删到一半炸掉就在暂存里留下一份孤儿副本，下一次打包还会被套进
`runs/runs/`（2026-11-02 实测踩到）。

⚠️ **打包时那个 exe 不能开着**：它占着 `runs/<tag>/_console.log`，Windows 不许改名/删除
（`WinError 32`）。真遇到了会**当场停下并说清怎么办**，不会硬删；已经在跑的包要打新包，
就用 `--out` 换个落点（见下）。

## 用户拿到手长这样

    sxagent/
    ├─ sxagent.exe
    ├─ .env              ← **你填好再发**（key / model / base_url）；随包只放 .env.example
    ├─ skills/           ← 首次运行自动建，并放 3 个示例技能；用户往里丢 .md 就加技能
    └─ runs/             ← 首次运行自动建；每次对话一份记录，让他们整包发回来
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "sxagent"
ENTRY = ROOT / "app" / "chat.py"
DIST = ROOT / "dist" / NAME   # 默认落点；`--out` 可以换（上一个包还在跑时必须换）

#: 随包带的**只读资源** —— `skills/` 下每个包是首次运行要铺给用户的示例技能。
#: 落点必须和 `proto.config.RESOURCE_ROOT` 的约定对上（冻结后 = `sys._MEIPASS`）。
ADD_DATA: list[tuple[str, str]] = [
    ("skills", "skills"),
]

#: 🔴 **动态导入重灾区** —— 不加这些，打出来的包会在用户那里报 ImportError。
#: `tiktoken` 靠 entry point 动态找 `tiktoken_ext.openai_public`，是最经典的漏收点。
COLLECT_ALL: list[str] = [
    "tiktoken",
    "langchain_openai",
]
COLLECT_SUBMODULES: list[str] = [
    "langchain_core",
    "langgraph",
    "langchain",
]

#: 明确不用的重家伙，排掉省体积（排错了会在运行时报 ImportError，所以只排确定不用的）。
EXCLUDE: list[str] = [
    "tkinter",
    "matplotlib",
    "pytest",
    "PyInstaller",
]

#: 🔴 **重建时必须保住的东西** —— PyInstaller 的 `--noconfirm` 会把目标目录**整个删掉**再重建，
#: 而这几样是**用户的数据**，不是构建产物（2026-11-02 实测踩到：打完 `.env` 没了，里面是真 key）。
#: 打包前挪到 `<dist>/_keep-<时间>`，打完（**哪怕打失败了**）原样放回。
#: ⚠️ `logs/` 不在列 —— 那是原始 trace，`runs/<tag>/` 里的 `_run.md` 已经附了一份。
KEEP: list[str] = [
    ".env",    # 填好的 key / model / base_url —— 丢了包就跑不起来
    "skills",  # 用户自己往里丢的技能包
    "runs",    # 每次对话的记录（收回来分析用的）
]


def _stash_user_data(built: Path, stash: Path, moved: list[str]) -> None:
    """把 `built` 里属于用户的数据挪进 `stash`（`KEEP` 里没有的就跳过）。

    ⚠️ **挪走了哪些记在 `moved` 里**（由调用方传进来）—— 挪到一半失败时，
    已经挪走的那几样还得靠它在 `finally` 里放回去。
    """
    for name in KEEP:
        src = built / name
        if not src.exists():
            continue
        stash.mkdir(parents=True, exist_ok=True)
        try:
            # ⚠️ 用 `os.rename`（同卷改名，原子）**不用 `shutil.move`** —— `move` 在改名失败时
            # 会回退成"先复制、再删源"：删源删到一半炸掉，暂存里就留下**一份孤儿副本**，
            # 下一次打包撞上它，还会被套进 `runs/runs/`（2026-11-02 实测踩到）。
            # 两边都在 `dist/` 下，同卷改名根本用不上那条回退。
            os.rename(src, stash / name)
        except OSError as e:
            # 🔴 实测踩到（2026-11-02）：**打好的 exe 还开着**，它占着 `runs/` 里的 `_console.log`，
            # Windows 不许改名 → `PermissionError: [WinError 32]`。
            # **这里不硬来**：说清怎么办，已经挪走的交给 `finally` 放回去。
            raise RuntimeError(
                f"挪不动 {src}（{e}）\n"
                f"  ⚠️ 多半是**打好的 exe 还在跑** —— 它占着 `runs/` 里的文件。\n"
                f"     先把它关掉；不想关就换个落点：`--out dist/_new`"
            ) from e
        moved.append(name)


def _restore_user_data(built: Path, stash: Path, moved: list[str]) -> None:
    """原样放回去。

    ⚠️ 落点上已经有同名东西时**合并不覆盖** —— 只有"打包中途失败"才会遇到，
    而那时落点上的东西（正在跑的 exe 刚写下的记录）**同样是用户的数据**，
    不能拿暂存里那份旧的盖掉。
    """
    for name in moved:
        src, dst = stash / name, built / name
        if not src.exists():
            continue
        if not dst.exists():
            os.rename(src, dst)
        elif dst.is_dir() and src.is_dir():
            for child in src.iterdir():
                if not (dst / child.name).exists():
                    os.rename(child, dst / child.name)
        # 单文件撞名（`.env`）：以**落点上已有的**为准，暂存那份留着 ——
        # 调用方看到暂存没清空会提示路径，不会一声不吭删掉。


def build(out_dir: Path | None = None) -> None:
    """`out_dir` 不传就打进 `dist/`。

    ⚠️ **上一个包还在跑的时候必须传它**（`--out dist/_new`）：PyInstaller 要**删掉**目标目录
    再重建，而正在运行的 exe 占着那个目录 → `PermissionError: [WinError 5] 拒绝访问`。
    """
    dist = out_dir or (ROOT / "dist")
    # ⚠️ 落点用 `dist / NAME`（实际产物），**不是**模块级的 `DIST` ——
    # 传了 `--out` 时两者不是一回事（踩过：提示打的是默认路径，实际打到了 `--out` 那儿）。
    built = dist / NAME
    # 暂存目录**每次换一个名字**（`_keep-1102-093512`）：固定名字会跟"上一次没放回去的暂存"
    # 撞上，而那种时候更要紧的是**别覆盖它** —— 宁可多留一个目录，也不能把用户的东西盖掉。
    stash = dist / f"_keep-{time.strftime('%m%d-%H%M%S')}"
    args: list[str] = [
        sys.executable, "-m", "PyInstaller",
        str(ENTRY),
        "--name", NAME,
        "--onedir",              # ← 见模块 docstring：不要改成 onefile
        "--console",             # 双击弹一个控制台窗口跑 chat —— 不用用户敲命令行
        "--noconfirm",
        "--clean",
        "--distpath", str(dist),
        "--workpath", str(ROOT / "build" / dist.name),  # 临时目录也跟着分开，免得互相踩
        "--paths", str(ROOT),    # 让 `app` / `evals` / `proto` 三个包可被解析
    ]
    for src, dst in ADD_DATA:
        args += ["--add-data", f"{src}{';' if sys.platform == 'win32' else ':'}{dst}"]
    for m in COLLECT_ALL:
        args += ["--collect-all", m]
    for m in COLLECT_SUBMODULES:
        args += ["--collect-submodules", m]
    for m in EXCLUDE:
        args += ["--exclude-module", m]

    kept: list[str] = []
    try:
        _stash_user_data(built, stash, kept)
        if kept:
            print(f"先把用户数据挪到一边（打完放回来）：{'、'.join(kept)}")
        print("打包中（几分钟）……")
        subprocess.run(args, cwd=ROOT, check=True)

        # 随包放一份 `.env.example` 当模板 —— **绝不拷真的 `.env`**（那是密钥）。
        example = ROOT / ".env.example"
        if example.is_file():
            shutil.copy2(example, built / ".env.example")
            print(f"\n已放入模板：{built / '.env.example'}")
    finally:
        # 🔴 放回放在 `finally`：**打失败时更要放** —— `check=True` 一抛异常、
        # 甚至上面**挪到一半**就炸（`kept` 是逐个追加的），都不能让用户的 `.env`
        # 留在暂存目录里。那种"悄悄没了"正是要避免的。
        if kept:
            built.mkdir(parents=True, exist_ok=True)
            try:
                _restore_user_data(built, stash, kept)
                print(f"用户数据已放回：{'、'.join(kept)}")
            except OSError as e:
                print(f"⚠️ 放回时出错：{e}")
        # ⚠️ **只有真放空了才删暂存目录** —— 里面但凡还剩东西，宁可留着让人自己拿。
        if stash.exists():
            if any(stash.iterdir()):
                print(f"⚠️ 有东西没能放回，暂存在（没删）：{stash}")
            else:
                stash.rmdir()
    print(
        f"\n完成：{built}\n"
        f"  ⚠️ 发出去之前把 `.env` 填好放进这个目录（模板已给）\n"
        f"  ⚠️ 告诉用户**整个文件夹拷走**，别只拷 exe"
    )


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="打包成能发给别人的 exe")
    ap.add_argument(
        "--out", type=Path, default=None,
        help="产物落到哪个 dist 目录（默认 dist/）。⚠️ **上一个包还在跑时必须指定**，"
             "否则 PyInstaller 删不掉目标目录会报 PermissionError",
    )
    build(ap.parse_args().out)
