"""HTTP client for llama-server (Windows side, OpenAI-compatible API).

The agent process never launches or manages the server: llama-server.exe
dies immediately (exit 53) when started from WSL2, so the user starts it
on Windows and mirrored networking exposes it on localhost (plan §4).

Constrained decoding: llama.cpp's server supports OpenAI-style
response_format json_schema, which we use for every structured call so a
malformed-JSON reply is impossible by construction (plan §7.3).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:8080"


class LlamaServerError(RuntimeError):
    pass


class LlamaClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 300.0,
        model: str | None = None,
        extra_payload: dict | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.model = model
        # merged into every request — e.g. {"chat_template_kwargs":
        # {"enable_thinking": False}} to switch off Qwen3.5 thinking mode
        self.extra_payload = extra_payload or {}

    def _resolve_model(self) -> str | None:
        """Router-mode servers require a model name; discover one if needed."""
        if self.model:
            return self.model
        try:
            req = urllib.request.Request(self.base_url + "/v1/models")
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("id") for m in data.get("data", []) if m.get("id")]
            if models:
                self.model = models[0]
        except OSError:
            pass
        return self.model

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise LlamaServerError(
                f"llama-server unreachable at {self.base_url} — start it on the "
                f"Windows side first ({e})"
            ) from e

    def health(self) -> bool:
        try:
            req = urllib.request.Request(self.base_url + "/health")
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return resp.status == 200
        except OSError:
            return False

    def complete_json(
        self,
        messages: list[dict],
        schema: dict,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> dict:
        """Chat completion whose reply is forced to match `schema`."""
        payload = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "reply", "schema": schema, "strict": True},
            },
        }
        model = self._resolve_model()
        if model:
            payload["model"] = model
        payload.update(self.extra_payload)
        data = self._post("/v1/chat/completions", payload)
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
            return json.loads(content)
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            finish = None
            try:
                finish = data["choices"][0].get("finish_reason")
            except Exception:
                pass
            detail = f"finish_reason={finish}" if finish else f"payload={str(data)[:400]}"
            raise LlamaServerError(f"completion unusable ({e}); {detail}") from e
