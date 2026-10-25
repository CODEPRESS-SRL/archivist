"""Keyword normalization: lowercase, slug, filter, collapse, truncate."""

from __future__ import annotations

import pytest

from app.utils.keywords import normalize_keyword

# (raw, expected) — table-driven cases covering the binding format.
CASES: list[tuple[str, str]] = [
    ("Billing API", "billing-api"),  # spaces -> -, lowercase
    ("  Billing  ", "billing"),  # trim + inner collapse
    ("snake_case_key", "snake-case-key"),  # underscores -> -
    ("CamelCaseKeyword", "camelcasekeyword"),  # CamelCase -> lowercase, no split
    ("café", "caf"),  # accents dropped (é not in [a-z0-9-])
    ("naïve café", "nave-caf"),  # accents dropped, space -> -
    ("hello 🌍 world", "hello-world"),  # emoji dropped, collapse dashes
    ("a@b#c", "abc"),  # symbols dropped, letters kept
    ("--a--b--", "a-b"),  # leading/trailing/inner collapse
    ("v1.2.3", "v123"),  # dots dropped, digits kept
    ("UPPER_snake Mix", "upper-snake-mix"),  # mixed casing/separators
    ("x" * 65, "x" * 64),  # 65 chars truncated to 64
]

VALUE_ERROR_CASES: list[str] = [
    "",  # empty string
    "   ",  # whitespace only
    "@#$%",  # symbols only -> empty after filter
    "🌍🚀",  # emoji only -> empty
    "___",  # separators only -> empty after strip
]


@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_normalize_keyword(raw: str, expected: str) -> None:
    assert normalize_keyword(raw) == expected


@pytest.mark.parametrize("raw", VALUE_ERROR_CASES)
def test_normalize_keyword_empty_result_raises(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_keyword(raw)


@pytest.mark.parametrize("raw", [raw for raw, _ in CASES])
def test_normalize_keyword_is_idempotent(raw: str) -> None:
    once = normalize_keyword(raw)
    assert normalize_keyword(once) == once


def test_truncation_boundary_is_64_chars() -> None:
    result = normalize_keyword("a" * 100)
    assert len(result) == 64


def test_truncation_does_not_leave_trailing_dash() -> None:
    # 63 'a', then a dash that would fall on index 63 before truncation;
    # truncation must not leave a trailing '-' at the boundary.
    raw = "a" * 63 + "-b"
    result = normalize_keyword(raw)
    assert not result.endswith("-")
    assert len(result) <= 64
