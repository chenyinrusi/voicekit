"""LLM 适配层：标准库直连 OpenAI 兼容接口（/chat/completions）。

为什么自己写：CAS 的 `src.llm.*` 依赖私有仓库；这一层只保留引擎需要的
`Message` / `create_llm` / `ensure_provider_sdks_loaded` / `release_llm_client` 四个符号。

环境变量（都可覆盖 provider 默认值）：
- `VOICEKIT_BASE_URL`、`VOICEKIT_API_KEY`、`VOICEKIT_MODEL`

内置 provider：deepseek（默认）、openai、moonshot。
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

#: provider -> (base_url, api key env, default model)
PROVIDERS: dict[str, tuple[str, str, str]] = {
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "deepseek-chat"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-4o"),
    "moonshot": ("https://api.moonshot.cn/v1", "MOONSHOT_API_KEY", "moonshot-v1-8k"),
}


@dataclass
class Message:
    """最小的消息对象；引擎只用到 role/content 与三个类方法。"""

    role: str
    content: str

    @classmethod
    def system(cls, content: str) -> "Message":
        return cls("system", content)

    @classmethod
    def user(cls, content: str) -> "Message":
        return cls("user", content)

    @classmethod
    def assistant(cls, content: str) -> "Message":
        return cls("assistant", content)

    def as_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


def _settings(provider: str, model: str | None) -> tuple[str, str, str]:
    if provider not in PROVIDERS:
        raise SystemExit(f"unknown provider {provider!r}; known: {', '.join(PROVIDERS)}")
    base, key_env, default_model = PROVIDERS[provider]
    base = os.environ.get("VOICEKIT_BASE_URL") or base
    key = os.environ.get("VOICEKIT_API_KEY") or os.environ.get(key_env, "")
    model = model or os.environ.get("VOICEKIT_MODEL") or default_model
    if not key:
        raise SystemExit(f"missing API key: set {key_env} (or VOICEKIT_API_KEY)")
    return base.rstrip("/"), key, model


@dataclass
class _Response:
    """一次回复：正文 + usage。判定侧按 `.content` / `.usage` 取值，别退回裸字符串。"""

    content: str
    usage: dict


class _Client:
    def __init__(self, provider: str, model: str | None) -> None:
        self.base, self.key, self.model = _settings(provider, model)

    def _post(self, messages: list[dict], temperature: float, timeout: float) -> _Response:
        body = json.dumps(
            {"model": self.model, "messages": messages, "temperature": temperature}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # 把服务端报文带出来，别吞成一句 500
            detail = exc.read().decode("utf-8", "replace")[:800]
            raise SystemExit(f"LLM HTTP {exc.code}: {detail}") from exc
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise SystemExit(f"unexpected LLM payload: {str(payload)[:800]}") from exc
        usage = payload.get("usage")
        return _Response(content=str(content or ""), usage=dict(usage or {}))

    async def chat(self, messages: list, temperature: float = 0.9, timeout: float | None = None) -> _Response:
        payload = [m.as_dict() if isinstance(m, Message) else dict(m) for m in messages]
        return await asyncio.to_thread(
            self._post, payload, temperature, timeout if timeout is not None else 180.0
        )


def create_llm(provider: str, model: str | None = None) -> _Client:
    return _Client(provider, model)


async def ensure_provider_sdks_loaded() -> None:
    """兼容垫片：CAS 里用来懒加载 SDK，这里只用标准库。"""
    return None


async def release_llm_client(llm: object) -> None:
    """兼容垫片：标准库连接随请求结束释放。"""
    return None
