"""Deterministic ground-truth fact extraction from news text (regex only).

Facts are the recall ground truth: each fact's ``value`` is used verbatim as the
probe pattern (``re.escape(value)``), so it MUST be a token that appears
literally in the article body — otherwise the probe can never match even a
perfect memory. Adapted 2026-07-10 to the real backend_news corpus (English
Bloomberg-style financial news): the dominant checkable tokens are percentages
("12%") and dollar amounts ("$29 billion"); dividends are kept (rare) and
ratings remain semantic (non-verbatim, so recall-weak — rare in this corpus).
"""
from __future__ import annotations

import re

from benchmark.types import Fact

_DIVIDEND = re.compile(r"dividend of\s*\$?\s*(\d+(?:\.\d+)?)", re.I)
_DOWNGRADE = re.compile(r"\b(downgrad\w*|cut to|lowered to|underweight)\b", re.I)
_UPGRADE = re.compile(r"\b(upgrad\w*|raised to|overweight|outperform)\b", re.I)
# Verbatim numeric tokens — the values that actually survive in a refined memory.
_PCT = re.compile(r"\d+(?:\.\d+)?\s?%")
_USD = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:billion|million|trillion|bn|mn))?", re.I)


def extract_facts(title: str, body: str) -> tuple[Fact, ...]:
    text = f"{title}. {body}"
    facts: list[Fact] = []

    if m := _DIVIDEND.search(text):
        facts.append(Fact(type="dividend", value=m.group(1), unit="USD"))
    if _DOWNGRADE.search(text):
        facts.append(Fact(type="rating", value="downgrade"))
    elif _UPGRADE.search(text):
        facts.append(Fact(type="rating", value="upgrade"))
    if m := _PCT.search(text):
        # verbatim, whitespace-collapsed: "4.2 %" -> "4.2%"
        facts.append(Fact(type="pct", value=re.sub(r"\s+", "", m.group(0)), unit="%"))
    if m := _USD.search(text):
        # verbatim, whitespace-collapsed: "$29  billion" -> "$29 billion"
        facts.append(Fact(type="usd", value=re.sub(r"\s+", " ", m.group(0).strip())))

    return tuple(facts)
