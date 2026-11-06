"""模型配置：全部走环境变量 / .env，不写死在代码里。

    MODEL_NAME      模型名，如 deepseek-chat / qwen-max / glm-4-plus / gpt-4o-mini
    MODEL_API_KEY   API Key
    MODEL_BASE_URL  兼容端点，如 https://api.deepseek.com/v1（官方 OpenAI 可不填）
    MODEL_TIMEOUT_S 单次调用的**读超时**（秒，默认 300）—— 卡住时的闸门，
                    见 `get_model` 里那条注释〔沿革 3〕（它治的是"一次阻塞调用"，不是墙钟）

## 这个文件是什么

配置与运行环境的唯一出处：`.env` 从哪读（`app_root`）、冻结后随包带的只读资源在哪
（`RESOURCE_ROOT`）、三个标准流的编码、以及 `get_model()` 造出来的那个 OpenAI 兼容
chat model。**不碰任何业务**。

## 设计原则（不变量）

1. **路径一律现算，不靠 cwd** —— 打包后"从哪双击"都不该影响读不读得到配置。
2. **三个流都设 UTF-8**（`stdin` / `stdout` / `stderr`）—— 缺哪个坏哪种，
   见 `load_dotenv` 之后那条注释〔沿革 2〕。
3. **只走真实 LLM 调用，没有 fake 开关** —— 想省额度就少跑几轮，别造假信号〔沿革 1〕。
4. **单次调用有读超时闸门**（`MODEL_TIMEOUT_S`）—— 墙钟治不了"一次阻塞调用"〔沿革 3〕。

## 沿革（改过什么、为什么改）

> 编号是**引用锚**（正文里写 `〔沿革 N〕`）—— **只追加、不改号**，新条目加在**末尾**。

1. **~~`PROTO_FAKE_MODEL`~~ 拆掉**（2026-10-28）。理由三条：
   ① 它只假了**执行器与判定**，主 agent 仍调真实 API —— 却对外宣称"不调真实模型"，
   让人以为在离线跑、实际在烧额度；
   ② 它**短路了 `run_task` 的整个轮次循环**（直接返回硬编码的 ExecOutcome），
   所以拿它测多轮行为、出口限制、判定视野**全都测不到**，反而给出"跑通了"的假信号；
   ③ 实测假模型与真模型行为差异很大（真模型会"将就着做"、会"吹毛求疵"），
   **提示词与判定的效果只有真模型能测出来**。
2. **打包成 exe 时补的两条**（2026-10-30）。① `app_root()` **不能靠 cwd**：实测过 ——
   `cd` 到别的目录再 import，`load_dotenv()` 就读不到 `.env`（它从 **cwd** 往上找，
   不是从代码位置找）；双击 exe 时 cwd 也不保证是 exe 目录。② **三个流都要设 UTF-8**
   （打包后实测踩过两次）：**stderr** 缺了，报错信息落回系统编码（中文 Windows 上是
   GBK），于是"报错"和"正常输出"在同一份重定向里是**两种编码** —— 最早的中文输出就是
   本模块抛的"缺少模型配置"，它走 stderr，而入口那行 `reconfigure` 还没执行到就已经
   打出来了；**stdin** 缺了，**管道 / 重定向喂进来的中文按 GBK 解**，解不了的字变成
   **代理字符**，接着写日志时 `UnicodeEncodeError` —— **一条坏字符整场崩掉**
   （真实控制台走 Windows API 不受影响，所以这个坑只在"喂输入"时暴露）。
3. **单次调用的读超时**（2026-11-06，外审 F9 的核实）。原来不传 —— 于是吃 openai SDK
   的默认值（**read=600s ＋ 内部重试 2 次**），再叠上 `trace.stream_agent` 自己那一次
   重试：一次"卡住"最坏能拖**几十分钟**，而报告写的"数分钟"是低估。
   **墙钟（在轮次之间查表）治不了它** —— 卡住的是**一次阻塞调用**，轮次边界根本走不到，
   真正的闸门在这儿。⚠️ 它是**读间隔**超时（httpx 的 read timeout 按"两次读到数据之间"
   算）—— 所以**一直在吐字的流不会被误杀**，只有真卡住才触发 ✓。
   ⚠️ 触发后走既有故障路：`APITimeoutError` 没有 `status_code` → `transient`
   → 回执说"原样再派"（同 SX-07 那套分类，不新增分支）。
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


def app_root() -> Path:
    """**应用根** —— 打包成 exe 后是 **exe 所在目录**，开发时是仓库根（`langgraph-lab/`）。

    🔴 **不能靠 cwd**（实测踩过，见〔沿革 2〕）：`load_dotenv()` 从 **cwd** 往上找，
    不是从代码位置找 —— 而双击 exe 时 cwd 也不保证是 exe 目录。

    `sys.frozen` 是 PyInstaller 打的包会设的标记；`sys.executable` 那时就是 exe 自己。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]  # proto/ 的上一层 = langgraph-lab/


#: 冻结后随包带的只读资源在 `sys._MEIPASS`（PyInstaller 的解包目录），开发时就是应用根。
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "_MEIPASS", "") else app_root()

#: **显式指定路径**，不让它去猜 cwd —— 打包后"从哪双击"都不该影响读不读得到配置。
load_dotenv(app_root() / ".env")

# 🔴 **三个流都要设 UTF-8**（打包后实测踩过两次）：缺哪个坏哪种 ——
#   stderr 缺了报错落回系统编码、stdin 缺了管道喂进来的中文按 GBK 解（细节见〔沿革 2〕）。
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 —— 老终端/特殊流不支持就算了，不该因此起不来
        pass


@lru_cache(maxsize=8)
def get_model(temperature: float = 0.0):
    """返回一个 OpenAI 兼容的 chat model（同一温度只建一次）。"""
    from langchain_openai import ChatOpenAI

    name = os.getenv("MODEL_NAME", "").strip()
    key = os.getenv("MODEL_API_KEY", "").strip()
    base = os.getenv("MODEL_BASE_URL", "").strip()

    if not name or not key:
        # ⚠️ 路径要**现算**：打包后 `.env` 在 exe 旁边，写死 "langgraph-lab/.env"
        # 会把人指到一个根本不存在的目录。
        raise SystemExit(
            f"\n缺少模型配置。请在 {app_root() / '.env'} 里填：\n"
            "  MODEL_NAME      模型名\n"
            "  MODEL_API_KEY   API Key\n"
            "  MODEL_BASE_URL  兼容端点（可选）\n"
            "\n（本原型没有 fake 模式 —— 见模块 docstring 的说明。）\n"
        )
    # 🔴 **单次调用的读超时**（默认 300s）—— 卡住时的闸门在这儿，不在墙钟：
    #    墙钟在轮次之间查表，而卡住的是**一次阻塞调用**，轮次边界根本走不到。
    #    取值、误杀边界、触发后走哪条故障路见〔沿革 3〕。
    timeout_s = float(os.getenv("MODEL_TIMEOUT_S", "") or 300)
    return ChatOpenAI(
        model=name,
        api_key=key,  # type: ignore[arg-type]
        base_url=base or None,
        temperature=temperature,
        timeout=timeout_s,  # type: ignore[arg-type]
    )


def describe() -> str:
    """返回一行配置描述，便于运行时确认用的哪个模型。"""
    name = os.getenv("MODEL_NAME", "?")
    base = os.getenv("MODEL_BASE_URL", "").strip() or "api.openai.com"
    return f"{name} @ {base}"
