"""Opt-in, single-attempt DeepSeek HTTP client; no SDK or secret logging."""

import json
import os
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic

import httpx

from src.api_models import ModelTokenUsage


DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-flash"
MAX_OUTPUT_TOKENS = 2048
MAX_RESPONSE_BYTES = 128 * 1024
MAX_PROMPT_CHARS = 10_000
REQUEST_TIMEOUT_SECONDS = 30


class GenerationError(ValueError):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code


@dataclass(frozen=True)
class DeepSeekSettings:
    enabled: bool = False
    api_key: str | None = field(default=None, repr=False)
    max_calls: int = 10

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("DeepSeek enabled 必须是布尔值")
        if type(self.max_calls) is not int or not 1 <= self.max_calls <= 100:
            raise ValueError("MATHSCOPE_DEEPSEEK_MAX_CALLS 必须是 1 到 100 的整数")
        # Never include the offending credential in errors or dataclass repr.
        if self.api_key is not None and (
            not isinstance(self.api_key, str) or not 1 <= len(self.api_key) <= 256
            or not self.api_key.isascii() or any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in self.api_key)
        ):
            raise ValueError("DEEPSEEK_API_KEY 格式不合法；请在本机重新配置，不要发送密钥")

    @classmethod
    def from_env(cls, environ=None):
        values = os.environ if environ is None else environ
        enabled = values.get("MATHSCOPE_DEEPSEEK_ENABLED", "0")
        if enabled not in ("0", "1"):
            raise ValueError("MATHSCOPE_DEEPSEEK_ENABLED 必须是 0 或 1")
        raw_limit = values.get("MATHSCOPE_DEEPSEEK_MAX_CALLS", "10")
        if not isinstance(raw_limit, str) or not raw_limit.isascii() or not raw_limit.isdecimal():
            raise ValueError("MATHSCOPE_DEEPSEEK_MAX_CALLS 必须是整数字符串")
        # Disabled applications don't load an unrelated credential.
        return cls(enabled=enabled == "1", api_key=values.get("DEEPSEEK_API_KEY") if enabled == "1" else None,
                   max_calls=int(raw_limit))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-finite JSON number")


def strict_json_loads(value):
    return json.loads(value, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


@dataclass(frozen=True)
class ModelCompletion:
    draft: object
    usage: dict


class DeepSeekClient:
    def __init__(self, settings: DeepSeekSettings, *, transport=None):
        if not isinstance(settings, DeepSeekSettings):
            raise TypeError("settings 必须是 DeepSeekSettings 对象")
        self.settings = settings
        self._transport = transport  # MockTransport in offline tests, never a request parameter.
        self._lock = Lock()
        self._calls_used = 0
        self._in_flight = False

    def call_state(self):
        with self._lock:
            return {"calls_used": self._calls_used, "max_calls": self.settings.max_calls}

    def public_status(self):
        """Local readiness snapshot only; never validates a key with the provider."""
        with self._lock:
            status = ("disabled" if not self.settings.enabled else
                      "missing_key" if not self.settings.api_key else
                      "busy" if self._in_flight else
                      "limit_reached" if self._calls_used >= self.settings.max_calls else "ready")
            return {"provider": "deepseek", "model": DEEPSEEK_MODEL, "status": status,
                    "calls_used": self._calls_used, "max_calls": self.settings.max_calls,
                    "credential_verified": False}

    def complete(self, messages):
        if not self.settings.enabled or not self.settings.api_key:
            raise GenerationError(503, "DeepSeek 尚未启用或密钥未配置；请在服务器终端配置并重启")
        if not isinstance(messages, list) or len(messages) != 2 or any(
            not isinstance(item, dict) or not isinstance(item.get("content"), str) for item in messages
        ) or sum(len(item["content"]) for item in messages) > MAX_PROMPT_CHARS:
            raise GenerationError(422, "模型输入超过当前字符上限或格式不合法")
        with self._lock:
            if self._in_flight:
                raise GenerationError(429, "已有模型请求进行中，请等待完成；不要重复提交")
            if self._calls_used >= self.settings.max_calls:
                raise GenerationError(429, "当前服务实例已达到模型调用次数上限；请先核对平台账单")
            self._in_flight = True
            # Attempts count even when timed out or rejected: they may be billed.
            self._calls_used += 1
        try:
            return self._request(messages)
        finally:
            with self._lock:
                self._in_flight = False

    def _request(self, messages):
        payload = {
            "model": DEEPSEEK_MODEL, "messages": messages, "stream": False,
            "thinking": {"type": "disabled"}, "temperature": 0,
            "response_format": {"type": "json_object"}, "max_tokens": MAX_OUTPUT_TOKENS,
        }
        started = monotonic()
        try:
            # Fixed official destination. No redirects, inherited proxies, SDK retries,
            # tools, remote files, or implicit HTTP request logging.
            with httpx.Client(
                transport=self._transport, follow_redirects=False, trust_env=False,
                timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=5, write=10, pool=5),
            ) as client:
                with client.stream("POST", DEEPSEEK_URL, json=payload, headers={
                    "Authorization": "Bearer " + self.settings.api_key,
                    "Content-Type": "application/json", "Accept": "application/json",
                }) as response:
                    if response.status_code in (401, 403):
                        raise GenerationError(503, "DeepSeek 密钥无效或没有权限；请在本机检查，不要发送密钥")
                    if response.status_code == 402:
                        raise GenerationError(503, "DeepSeek 账户余额不足；请在官方平台核对账单")
                    if response.status_code == 429:
                        raise GenerationError(503, "DeepSeek 暂时限流；本次不自动重试")
                    if response.status_code != 200:
                        raise GenerationError(502, "DeepSeek 请求失败；本次不自动重试")
                    parts, size = [], 0
                    for part in response.iter_bytes():
                        if monotonic() - started > REQUEST_TIMEOUT_SECONDS:
                            raise GenerationError(504, "DeepSeek 请求超时；可能已产生费用，本次不自动重试")
                        size += len(part)
                        if size > MAX_RESPONSE_BYTES:
                            raise GenerationError(502, "DeepSeek 响应超过当前读取上限")
                        parts.append(part)
        except httpx.TimeoutException:
            raise GenerationError(504, "DeepSeek 请求超时；可能已产生费用，本次不自动重试") from None
        except httpx.HTTPError:
            raise GenerationError(502, "无法连接 DeepSeek；请检查网络，本次不自动重试") from None
        try:
            data = strict_json_loads(b"".join(parts))
            if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or len(data["choices"]) != 1:
                raise ValueError("invalid choices")
            choice = data["choices"][0]
            if choice["finish_reason"] != "stop":
                raise ValueError("incomplete answer")
            message = choice["message"]
            if message["role"] != "assistant" or message.get("tool_calls"):
                raise ValueError("unexpected message")
            content = message["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty answer")
            usage = ModelTokenUsage.model_validate(data["usage"]).model_dump()
            if usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
                raise ValueError("inconsistent usage")
            return ModelCompletion(draft=strict_json_loads(content), usage=usage)
        except (ValueError, TypeError, KeyError, IndexError, RecursionError):
            raise GenerationError(502, "DeepSeek 返回空白、截断或不合法的 JSON/用量；不交付草稿且不自动重试") from None
