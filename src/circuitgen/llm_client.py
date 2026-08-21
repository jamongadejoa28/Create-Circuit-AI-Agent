"""HTTP client for llama-server (Windows side, OpenAI-compatible API).

The agent process never launches or manages the server: llama-server.exe
dies immediately (exit 53) when started from WSL2, so the user starts it
on Windows and mirrored networking exposes it on localhost.

Constrained decoding: llama.cpp's server supports OpenAI-style
response_format json_schema, which we use for every structured call so a
malformed-JSON reply is rejected at the client boundary.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:8080"


class LlamaServerError(RuntimeError):
    pass


class TruncatedCompletionError(LlamaServerError):
    """The model ran out of output budget mid-reply (finish_reason=length).

    Distinct from a transient server error because the remedy is different:
    re-sending the identical request produces the identical truncation, so a
    caller must shrink the request instead of retrying it. Measured: a 132-pin
    MCU block burned all 4096 tokens enumerating no-connect pins and the two
    "attempts" were four byte-identical generations.

    Carries the partial text so the failure stays diagnosable — it used to be
    thrown away, which is why nobody could confirm what the model was emitting.
    """

    def __init__(self, message: str, partial_content: str = "", usage: dict | None = None):
        super().__init__(message)
        self.partial_content = partial_content
        self.usage = usage or {}


# llama-server per-slot context in this deployment (confirmed via /props).
# Prompt + reply must fit: a block prompt that left less room than the reply
# cap produced a hard "Context size has been exceeded" HTTP 500.
SLOT_CONTEXT_TOKENS = 8192
# Conservative on purpose: real block prompts measured 1.87-2.57 chars/token,
# the low end on pin-dense tables (a 169-pin MCU, a 484-pin FPGA). Estimating
# high would let the reply cap overrun the slot, which the server answers with
# an opaque HTTP 500 instead of an error anyone can act on.
_CHARS_PER_TOKEN = 2.0
_CONTEXT_MARGIN = 256
MIN_USEFUL_REPLY_TOKENS = 512


def estimate_prompt_tokens(content: str) -> int:
    """Upper-bound token estimate for a request body (+ the system message)."""
    return int(len(content) / _CHARS_PER_TOKEN) + 64


class PromptTooLargeError(RuntimeError):
    """A request cannot fit the model's context however it is answered."""




def output_budget(content: str, cap: int = 4096) -> int:
    """Tokens the reply may use without overrunning the slot context.

    Raises when the prompt leaves no room for even a minimal answer: sending
    it anyway gets an opaque HTTP 500 from llama-server, which reads as a
    server fault rather than "this request cannot fit".
    """
    estimated_prompt = estimate_prompt_tokens(content)
    room = SLOT_CONTEXT_TOKENS - estimated_prompt - _CONTEXT_MARGIN
    if room < MIN_USEFUL_REPLY_TOKENS:
        raise PromptTooLargeError(
            f"prompt needs ~{estimated_prompt} of {SLOT_CONTEXT_TOKENS} context tokens, "
            f"leaving {room} for the reply (minimum {MIN_USEFUL_REPLY_TOKENS}) — "
            f"trim the request instead of sending it"
        )
    return min(cap, room)


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
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                body = ""
            raise LlamaServerError(f"llama-server HTTP {e.code}: {body}") from e
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
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> dict:
        """Chat completion whose reply is forced to match `schema`.

        Greedy by default. The server's own default (0.8) never applied: this
        request sets temperature explicitly, so changing it server-side has no
        effect on the agent. Sampling buys nothing here — every reply is
        grammar-constrained structured data, and the variance it does buy is
        the "content roulette" the bench keeps measuring as run-to-run spread.
        """
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
        # Truncation is checked BEFORE parsing: a reply cut at the output cap
        # can still be valid JSON (the grammar closes the braces), and that
        # would be accepted as a complete circuit with components silently
        # missing. finish_reason used to be read only inside the parse-error
        # handler, so the success path could never see it.
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError) as e:
            raise LlamaServerError(f"completion unusable ({e}); payload={str(data)[:400]}") from e
        if choice.get("finish_reason") == "length":
            usage = data.get("usage") or {}
            raise TruncatedCompletionError(
                f"completion truncated at the output cap "
                f"(max_tokens={max_tokens}, usage={usage}); the reply is incomplete",
                partial_content=content,
                usage=usage,
            )
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise LlamaServerError(
                f"completion unusable ({e}); finish_reason={choice.get('finish_reason')}"
            ) from e
