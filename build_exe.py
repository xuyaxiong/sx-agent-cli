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

## 用户拿到手长这样

    sxagent/
    ├─ sxagent.exe
    ├─ .env              ← **你填好再发**（key / model / base_url）；随包只放 .env.example
    ├─ skills/           ← 首次运行自动建，并放 3 个示例技能；用户往里丢 .md 就加技能
    └─ runs/             ← 首次运行自动建；每次对话一份记录，让他们整包发回来
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "sxagent"
ENTRY = ROOT / "evals" / "chat.py"
DIST = ROOT / "dist" / NAME

#: 随包带的**只读资源** —— `evals/skills/*.md` 是首次运行要铺给用户的示例技能。
#: 落点必须和 `proto.config.RESOURCE_ROOT` 的约定对上（冻结后 = `sys._MEIPASS`）。
ADD_DATA: list[tuple[str, str]] = [
    ("evals/skills", "evals/skills"),
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


def build() -> None:
    args: list[str] = [
        sys.executable, "-m", "PyInstaller",
        str(ENTRY),
        "--name", NAME,
        "--onedir",              # ← 见模块 docstring：不要改成 onefile
        "--console",             # 双击弹一个控制台窗口跑 chat —— 不用用户敲命令行
        "--noconfirm",
        "--clean",
        "--paths", str(ROOT),    # 让 `evals` / `proto` 两个包可被解析
    ]
    for src, dst in ADD_DATA:
        args += ["--add-data", f"{src}{';' if sys.platform == 'win32' else ':'}{dst}"]
    for m in COLLECT_ALL:
        args += ["--collect-all", m]
    for m in COLLECT_SUBMODULES:
        args += ["--collect-submodules", m]
    for m in EXCLUDE:
        args += ["--exclude-module", m]

    print("打包中（几分钟）……")
    subprocess.run(args, cwd=ROOT, check=True)

    # 随包放一份 `.env.example` 当模板 —— **绝不拷真的 `.env`**（那是密钥）。
    example = ROOT / ".env.example"
    if example.is_file():
        shutil.copy2(example, DIST / ".env.example")
        print(f"\n已放入模板：{DIST / '.env.example'}")
    print(
        f"\n完成：{DIST}\n"
        f"  ⚠️ 发出去之前把 `.env` 填好放进这个目录（模板已给）\n"
        f"  ⚠️ 告诉用户**整个文件夹拷走**，别只拷 exe"
    )


if __name__ == "__main__":
    build()
