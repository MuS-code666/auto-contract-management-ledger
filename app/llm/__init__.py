"""ローカルLLMアダプター。

OpenAI互換のチャットAPIを提供するローカルサーバー（llama.cpp の llama-server、Ollama 等）へ接続する。
接続先は 127.0.0.1 / localhost / ::1 に限定し、それ以外のURLは設定時点で拒否する
（契約情報を外部へ送信しないため）。HTTPクライアントは環境変数のプロキシ設定を使用しない。
"""
from __future__ import annotations

import json
import time
from typing import Callable
from urllib.parse import urlparse

import httpx

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


class LlmUnavailable(Exception):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


class LlmFailed(Exception):
    pass


class LlmCancelled(Exception):
    pass


def validate_local_url(url: str) -> str:
    u = urlparse(url or "")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("URLの形式が正しくありません（例: http://127.0.0.1:8081/v1）")
    if u.hostname not in LOOPBACK_HOSTS:
        raise ValueError("LLMの接続先はこのPC内（127.0.0.1 / localhost）に限られます。外部のサーバーは指定できません。")
    return url.rstrip("/")


class LlmEngine:
    name = "base"

    def check(self) -> dict:
        raise NotImplementedError

    def complete_json(self, system: str, user: str, schema: dict, timeout: float,
                      cancel: Callable[[], bool] | None = None) -> str:
        raise NotImplementedError


class NoLlm(LlmEngine):
    name = "none"

    def check(self) -> dict:
        return {"ok": False, "message": "LLMによる抽出は無効に設定されています（ルール抽出のみ行います）。",
                "hint": "README の「ローカルLLMの導入」に従ってサーバーを起動し、設定画面で有効にしてください。"}

    def complete_json(self, *a, **k) -> str:
        raise LlmUnavailable("LLMは無効に設定されています。")


class OpenAICompatLlm(LlmEngine):
    name = "openai_compat"

    def __init__(self, base_url: str, model: str = "", temperature: float = 0.0):
        self.base_url = validate_local_url(base_url)
        self.model = model
        self.temperature = temperature

    def _client(self, timeout: float) -> httpx.Client:
        return httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0), trust_env=False)

    def check(self) -> dict:
        try:
            with self._client(10) as c:
                r = c.get(f"{self.base_url}/models")
            r.raise_for_status()
            data = r.json()
            models = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
        except httpx.ConnectError:
            return {"ok": False, "message": f"LLMサーバー（{self.base_url}）に接続できません。",
                    "hint": "start_llm.bat でLLMサーバーを起動してから再確認してください。"}
        except Exception as e:
            return {"ok": False, "message": f"LLMサーバーの応答が不正です（{type(e).__name__}）。",
                    "hint": "接続先URL（末尾 /v1）を確認してください。"}
        if self.model and models and self.model not in models:
            return {"ok": False, "message": f"モデル「{self.model}」がサーバーにありません。",
                    "hint": f"利用可能: {', '.join(m for m in models if m)}", "detail": {"models": models}}
        return {"ok": True, "message": "LLMサーバーに接続できました。", "detail": {"models": models}}

    def complete_json(self, system: str, user: str, schema: dict, timeout: float,
                      cancel: Callable[[], bool] | None = None) -> str:
        body = {
            "model": self.model or "local",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.temperature,
            "stream": True,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "contract_extraction", "strict": True, "schema": schema}},
        }
        deadline = time.monotonic() + timeout
        parts: list[str] = []
        try:
            with self._client(timeout) as c:
                with c.stream("POST", f"{self.base_url}/chat/completions", json=body) as r:
                    if r.status_code >= 400:
                        r.read()
                        raise LlmFailed(f"LLMサーバーがエラーを返しました（HTTP {r.status_code}）")
                    for line in r.iter_lines():
                        if cancel and cancel():
                            raise LlmCancelled()
                        if time.monotonic() > deadline:
                            raise LlmFailed(f"LLMの応答がタイムアウトしました（{int(timeout)}秒）")
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            ev = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        for ch in ev.get("choices", []):
                            delta = (ch.get("delta") or {}).get("content")
                            if delta:
                                parts.append(delta)
        except httpx.ConnectError:
            raise LlmUnavailable(f"LLMサーバー（{self.base_url}）に接続できません。",
                                 "start_llm.bat でLLMサーバーを起動してください。")
        except httpx.TimeoutException:
            raise LlmFailed(f"LLMの応答がタイムアウトしました（{int(timeout)}秒）")
        return "".join(parts)


def get_engine(llm_settings: dict) -> LlmEngine:
    s = llm_settings or {}
    if s.get("engine") == "openai_compat":
        return OpenAICompatLlm(s.get("base_url", ""), s.get("model", ""), float(s.get("temperature", 0.0)))
    return NoLlm()
