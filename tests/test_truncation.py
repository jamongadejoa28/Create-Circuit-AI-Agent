"""Truncated completions must be loud, and the retry must not repeat itself.

The unknown_module MCU block burned its whole 4096-token output cap
enumerating no-connect pins. Two things were wrong beyond the token cost:
`finish_reason` was read only inside the JSON-parse error handler, so a
truncated-but-parseable reply would have been accepted as a complete circuit;
and both retry layers re-sent a byte-identical request, so the recorded "2
attempts" were four identical generations of the same doomed reply.
"""

import json

import pytest

from circuitgen.agent import (
    _CHARS_PER_TOKEN,
    PromptTooLargeError,
    SLOT_CONTEXT_TOKENS,
    _output_budget,
    _with_retry,
)
from circuitgen.llm_client import LlamaClient, LlamaServerError, TruncatedCompletionError


class FakeServer:
    """Stands in for LlamaClient._post; records the payloads it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.payloads = []

    def __call__(self, path, payload):
        self.payloads.append(payload)
        return self.replies.pop(0)


def _reply(content, finish_reason="stop", usage=None):
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": usage or {"prompt_tokens": 3902, "completion_tokens": 4096},
    }


def _client(server):
    client = LlamaClient(model="test-model")
    client._post = server
    return client


def test_truncated_reply_raises_even_when_the_partial_json_parses():
    # the grammar can close the braces on a cut-off reply: valid JSON, missing
    # half the circuit. This must never be returned as a complete answer.
    parseable = json.dumps({"name": "half", "components": [], "nets": []})
    server = FakeServer([_reply(parseable, finish_reason="length")])
    with pytest.raises(TruncatedCompletionError) as excinfo:
        _client(server).complete_json([], {"type": "object"}, max_tokens=4096)
    assert excinfo.value.partial_content == parseable
    assert excinfo.value.usage["completion_tokens"] == 4096
    assert "truncated" in str(excinfo.value)


def test_truncated_unparseable_reply_also_raises_the_typed_error():
    server = FakeServer([_reply('{"name": "half", "compo', finish_reason="length")])
    with pytest.raises(TruncatedCompletionError):
        _client(server).complete_json([], {"type": "object"}, max_tokens=4096)


def test_a_complete_reply_is_returned_normally():
    server = FakeServer([_reply(json.dumps({"ok": True}))])
    assert _client(server).complete_json([], {"type": "object"}) == {"ok": True}


def test_malformed_complete_reply_is_a_plain_server_error():
    server = FakeServer([_reply("not json at all")])
    with pytest.raises(LlamaServerError) as excinfo:
        _client(server).complete_json([], {"type": "object"})
    assert not isinstance(excinfo.value, TruncatedCompletionError)


def test_retry_can_send_a_different_request_each_attempt():
    seen = []

    def ask(attempt):
        seen.append(attempt)
        if attempt < 2:
            raise TruncatedCompletionError("truncated")
        return {"attempt": attempt}

    assert _with_retry(ask, tries=3, pass_attempt=True) == {"attempt": 2}
    assert seen == [0, 1, 2]


def test_retry_without_the_flag_keeps_the_old_zero_argument_contract():
    calls = []

    def ask():
        calls.append(1)
        if len(calls) == 1:
            raise LlamaServerError("hiccup")
        return {"ok": True}

    assert _with_retry(ask) == {"ok": True}
    assert len(calls) == 2


def test_exhausted_retries_raise_the_last_error():
    def ask(attempt):
        raise TruncatedCompletionError(f"truncated at level {attempt}")

    with pytest.raises(TruncatedCompletionError) as excinfo:
        _with_retry(ask, tries=3, pass_attempt=True)
    assert "level 2" in str(excinfo.value)


def test_output_budget_leaves_room_for_the_prompt():
    # A prompt big enough that a 4096-token reply would not fit produced a hard
    # "Context size has been exceeded" HTTP 500 rather than an answer.
    small = _output_budget("x" * 2000)
    medium = _output_budget("x" * 9730)
    assert small == 4096
    assert medium < 4096, "a large prompt must buy a smaller reply cap"
    for chars in (2000, 9730, 14000):
        assert _output_budget("x" * chars) + chars / _CHARS_PER_TOKEN < SLOT_CONTEXT_TOKENS


def test_a_prompt_that_cannot_fit_raises_instead_of_being_sent():
    """Returning a token floor sent an impossible request, and llama-server
    answers that with an opaque HTTP 500 that reads as a server fault."""
    with pytest.raises(PromptTooLargeError) as excinfo:
        _output_budget("x" * 20000)
    assert "trim the request" in str(excinfo.value)


def test_the_token_estimate_stays_conservative():
    """Real block prompts measured 1.87-2.57 chars/token, the low end on
    pin-dense tables. Estimating high lets the reply cap overrun the slot."""
    assert _CHARS_PER_TOKEN <= 2.0
