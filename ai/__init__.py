"""
ai 层：大模型调用封装与自然语言参数解析。

本层为纯逻辑层（只依赖 requests / config / core），不导入任何 Qt 代码，
便于在无界面环境下单独测试与复用。
"""

from ai.llm_client import LLMClient, LLMConfig, LLMError, create_client  # noqa: F401
from ai.param_interpreter import (  # noqa: F401
    EXAMPLE_PROMPTS,
    SYSTEM_PROMPT,
    InterpretResult,
    ParamInterpreter,
    coerce_params,
    extract_json,
    interpret_text,
    rule_based_parse,
)
