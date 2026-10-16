"""
大模型 API 封装
===============

职责：用 requests 通过 HTTP 调用大模型 API，统一封装成 OpenAI 兼容的
`/chat/completions` 协议，从而同时兼容豆包（火山方舟）、DeepSeek、通义千问
（DashScope 兼容模式），并预留任意自定义 base_url 的配置入口。

设计要点：
- 只依赖 requests，不引入任何厂商专属 SDK；
- 鉴权失败 / 模型不存在 / 频率超限等错误统一翻译成中文提示；
- 网络抖动自动重试（仅对超时、5xx、429 重试，401/403/404 直接失败）；
- 日志中绝不出现完整 API Key。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from config.settings import (
    LLM_MAX_RETRY,
    LLM_PROVIDERS,
    LLM_TEMPERATURE,
    LLM_TIMEOUT,
    settings,
)
from utils.logger import get_logger

logger = get_logger("ai.llm")


class LLMError(Exception):
    """大模型调用失败，message 始终是可直接展示给用户的中文提示。"""


@dataclass
class LLMConfig:
    """大模型连接配置。"""

    provider: str = "deepseek"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    timeout: int = LLM_TIMEOUT
    max_retry: int = LLM_MAX_RETRY
    temperature: float = LLM_TEMPERATURE

    @property
    def configured(self) -> bool:
        """密钥与模型是否已填写完整。"""
        return bool(self.api_key.strip() and self.model.strip() and self.base_url.strip())

    @property
    def provider_label(self) -> str:
        return LLM_PROVIDERS.get(self.provider, {}).get("label", self.provider)

    @property
    def masked_key(self) -> str:
        """日志用的掩码密钥（只保留前 4 位）。"""
        key = self.api_key.strip()
        if not key:
            return "(未配置)"
        return key[:4] + "*" * max(0, len(key) - 4)

    def endpoint(self) -> str:
        """拼接 OpenAI 兼容的 chat/completions 地址。"""
        return self.base_url.rstrip("/") + "/chat/completions"

    @classmethod
    def from_settings(cls) -> "LLMConfig":
        """
        从本地配置 + .env 环境变量构建配置。
        优先级：程序内设置 > .env 环境变量 > 厂商内置默认值。
        """
        provider = (
            str(settings.get("llm_provider") or "").strip().lower()
            or os.getenv("LLM_PROVIDER", "").strip().lower()
            or "deepseek"
        )
        if provider not in LLM_PROVIDERS:
            provider = "deepseek"
        profile = LLM_PROVIDERS[provider]

        api_key = str(settings.get("api_key") or "").strip() or os.getenv(profile["env_key"], "").strip()
        base_url = (
            str(settings.get("api_base_url") or "").strip()
            or os.getenv(profile["env_base"], "").strip()
            or profile["base_url"]
        )
        model = (
            str(settings.get("api_model") or "").strip()
            or os.getenv(profile["env_model"], "").strip()
            or profile["model"]
        )
        return cls(provider=provider, api_key=api_key, base_url=base_url, model=model)


class LLMClient:
    """OpenAI 兼容协议的大模型客户端。"""

    def __init__(self, config: Optional[LLMConfig] = None) -> None:
        self._config = config or LLMConfig.from_settings()
        self._session = requests.Session()

    # ---------------- 配置 ----------------
    @property
    def config(self) -> LLMConfig:
        return self._config

    @property
    def configured(self) -> bool:
        return self._config.configured

    def reload_config(self) -> None:
        """重新从本地配置 / 环境变量加载连接信息。"""
        self._config = LLMConfig.from_settings()
        logger.info(
            "已重新加载大模型配置：provider=%s model=%s key=%s",
            self._config.provider, self._config.model, self._config.masked_key,
        )

    # ---------------- 核心调用 ----------------
    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: int = 2048,
        response_format_json: bool = False,
    ) -> str:
        """
        发起一次对话请求并返回模型文本回复。

        :param messages: OpenAI 格式的消息列表
        :param response_format_json: 要求模型以 JSON 对象作答（部分接口不支持时自动降级重试）
        :raises LLMError: 未配置密钥、网络异常、鉴权失败、返回格式异常等
        """
        if not self._config.configured:
            raise LLMError("尚未配置大模型 API Key，请在【AI 设置】中填写后重试。")
        if not messages:
            raise LLMError("请求内容为空，无法调用大模型。")

        payload: Dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "temperature": self._config.temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if response_format_json:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        logger.info(
            "调用大模型：provider=%s model=%s json_mode=%s",
            self._config.provider, self._config.model, response_format_json,
        )

        last_error = ""
        attempts = max(0, self._config.max_retry) + 1
        for attempt in range(attempts):
            try:
                response = self._session.post(
                    self._config.endpoint(),
                    headers=headers,
                    json=payload,
                    timeout=self._config.timeout,
                )
            except requests.exceptions.Timeout:
                last_error = "【请求超时】调用大模型超时，请检查网络或代理设置后重试。"
            except requests.exceptions.RequestException as exc:
                last_error = f"【网络异常】无法连接大模型服务：{exc}"
            else:
                if response.status_code == 200:
                    return self._extract_content(response)

                # 部分厂商接口不支持 response_format 参数，去掉后重试一次
                if response.status_code == 400 and "response_format" in payload:
                    logger.warning("接口不支持 JSON 模式，去掉 response_format 后重试")
                    payload.pop("response_format", None)
                    continue

                last_error = self._humanize_http_error(response)
                if response.status_code in (400, 401, 403, 404):
                    # 请求本身有问题，重试无意义
                    raise LLMError(last_error)

            if attempt < attempts - 1:
                time.sleep(1.0 * (attempt + 1))

        raise LLMError(last_error or "调用大模型失败，请稍后重试。")

    def test_connection(self) -> Tuple[bool, str]:
        """连通性自测（供设置界面“测试连接”按钮使用）。"""
        if not self._config.configured:
            return False, "请先填写 API Key 与模型 ID，再测试连接。"
        try:
            reply = self.chat(
                [{"role": "user", "content": "请只回复两个字：正常"}],
                temperature=0.0,
                max_tokens=16,
            )
        except LLMError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001  自测按钮不应因未知异常崩溃
            return False, f"连接失败：{exc}"
        return True, f"连接成功，模型回复：{reply[:32]}"

    # ---------------- 内部工具 ----------------
    @staticmethod
    def _extract_content(response: requests.Response) -> str:
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMError("【返回格式异常】大模型返回内容无法解析为 JSON。") from exc

        try:
            choices = data.get("choices") or []
            content = choices[0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("【返回格式异常】大模型返回结构不符合预期，请重试或更换模型。") from exc

        if not isinstance(content, str) or not content.strip():
            raise LLMError("【空响应】大模型没有返回有效内容，请重试。")
        return content.strip()

    @staticmethod
    def _humanize_http_error(response: requests.Response) -> str:
        """把 HTTP 状态码翻译成中文，并附加服务端返回的错误摘要。"""
        detail = ""
        try:
            body = response.json()
            error = body.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or "")[:200]
            elif isinstance(error, str):
                detail = error[:200]
            if not detail:
                detail = str(body.get("message") or "")[:200]
        except ValueError:
            detail = (response.text or "")[:200]

        mapping = {
            400: "【请求被拒绝】接口不接受当前请求参数。",
            401: "【鉴权失败】API Key 无效或已过期，请在 AI 设置中重新填写。",
            403: "【无访问权限】当前账号没有该模型的调用权限，请检查开通状态。",
            404: "【模型不存在】模型 ID 填写有误；豆包需填写 ep- 开头的推理接入点 ID。",
            429: "【频率超限】调用过于频繁，请稍后再试。",
            500: "【服务端异常】大模型服务内部错误，请稍后重试。",
            502: "【服务端异常】网关错误，请稍后重试。",
            503: "【服务繁忙】大模型服务暂不可用，请稍后重试。",
        }
        message = mapping.get(response.status_code, f"【请求失败】HTTP {response.status_code}。")
        return f"{message} 服务端信息：{detail}" if detail else message


def create_client() -> LLMClient:
    """工厂函数：便捷创建一个按当前配置初始化的客户端。"""
    return LLMClient()
