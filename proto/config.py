"""模型配置：全部走环境变量 / .env，不写死在代码里。

    MODEL_NAME      模型名，如 deepseek-chat / qwen-max / glm-4-plus / gpt-4o-mini
    MODEL_API_KEY   API Key
    MODEL_BASE_URL  兼容端点，如 https://api.deepseek.com/v1（官方 OpenAI 可不填）

⚠️ **本原型只走真实 LLM 调用，没有 fake 开关。**

    曾经有过一个 `PROTO_FAKE_MODEL=1`，2026-10-28 拆掉了。理由：
      1. 它只假了**执行器与判定**，主 agent 仍调真实 API —— 却对外宣称"不调真实模型"，
         让人以为在离线跑、实际在烧额度；
      2. 它**短路了 `run_task` 的整个轮次循环**（直接返回硬编码的 ExecOutcome），
         所以拿它测多轮行为、出口限制、判定视野**全都测不到**，
         反而给出"跑通了"的假信号；
      3. 实测假模型与真模型行为差异很大（真模型会"将就着做"、会"吹毛求疵"），
         **提示词与判定的效果只有真模型能测出来**。

    想省额度就少跑几轮，别造假信号。
"""

from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@lru_cache(maxsize=8)
def get_model(temperature: float = 0.0):
    """返回一个 OpenAI 兼容的 chat model（同一温度只建一次）。"""
    from langchain_openai import ChatOpenAI

    name = os.getenv("MODEL_NAME", "").strip()
    key = os.getenv("MODEL_API_KEY", "").strip()
    base = os.getenv("MODEL_BASE_URL", "").strip()

    if not name or not key:
        raise SystemExit(
            "\n缺少模型配置。请在 langgraph-lab/.env 里填：\n"
            "  MODEL_NAME      模型名\n"
            "  MODEL_API_KEY   API Key\n"
            "  MODEL_BASE_URL  兼容端点（可选）\n"
            "\n（本原型没有 fake 模式 —— 见模块 docstring 的说明。）\n"
        )
    return ChatOpenAI(
        model=name,
        api_key=key,  # type: ignore[arg-type]
        base_url=base or None,
        temperature=temperature,
    )


def describe() -> str:
    """返回一行配置描述，便于运行时确认用的哪个模型。"""
    name = os.getenv("MODEL_NAME", "?")
    base = os.getenv("MODEL_BASE_URL", "").strip() or "api.openai.com"
    return f"{name} @ {base}"
