"""The chat contract capped every question at 4000 characters, so a pasted JSON
document was rejected with a 422 before the RAG's converter could ever see it."""

import json

import pytest
from pydantic import ValidationError

from app.api.chat_contracts import (
    MAX_PROSE_QUESTION_CHARACTERS,
    ChatRequest,
)
from app.conversations.store import MAX_STORED_TURN_CHARACTERS, _stored_text


def _big_object(entries: int = 4000) -> str:
    return json.dumps({f"key_{index}": index for index in range(entries)})


def test_a_large_json_object_is_accepted():
    payload = _big_object()
    assert len(payload) > MAX_PROSE_QUESTION_CHARACTERS
    assert ChatRequest(question=payload).question == payload


def test_a_large_fenced_payload_is_accepted():
    assert ChatRequest(question=f"```json\n{_big_object()}\n```") is not None


def test_long_prose_is_still_rejected():
    with pytest.raises(ValidationError, match="single JSON document"):
        ChatRequest(question="why " * 2000)


def test_prose_wrapped_around_a_big_payload_is_rejected():
    with pytest.raises(ValidationError):
        ChatRequest(question="explain all of this to me " * 200 + _big_object())


def test_short_prose_is_unaffected():
    assert ChatRequest(question="what does the bot application do?").mode == "ASK"


def test_a_stored_turn_is_truncated_with_a_visible_marker():
    stored = _stored_text("x" * (MAX_STORED_TURN_CHARACTERS + 500))
    assert stored.startswith("x" * MAX_STORED_TURN_CHARACTERS)
    assert "[truncated: 500 more characters]" in stored


def test_a_short_turn_is_stored_verbatim():
    assert _stored_text("hello") == "hello"
