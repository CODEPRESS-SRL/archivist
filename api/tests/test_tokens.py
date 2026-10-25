"""Token counting via tiktoken cl100k_base — no approximations."""

from __future__ import annotations

import tiktoken

from app.utils.tokens import count_tokens


def test_count_tokens_hello_world_matches_real_tiktoken() -> None:
    expected = len(tiktoken.get_encoding("cl100k_base").encode("hello world"))
    assert count_tokens("hello world") == expected


def test_count_tokens_empty_string_is_zero() -> None:
    assert count_tokens("") == 0


def test_count_tokens_matches_encoder_for_varied_text() -> None:
    encoder = tiktoken.get_encoding("cl100k_base")
    for text in ("a", "The quick brown fox.", "café 🌍", "snake_case_key"):
        assert count_tokens(text) == len(encoder.encode(text))
