"""Deterministic ground-truth fact extraction from news text (regex only)."""
from __future__ import annotations

import re

from benchmark.types import Fact

_DIVIDEND = re.compile(r"dividend of\s*\$?\s*(\d+(?:\.\d+)?)", re.I)
_DOWNGRADE = re.compile(r"\b(downgrad\w*|cut to|lowered to|underweight)\b", re.I)
_UPGRADE = re.compile(r"\b(upgrad\w*|raised to|overweight|outperform)\b", re.I)
_UP = re.compile(r"\b(rose|gained|rallied|jumped|climbed|surged)\s+(\d+(?:\.\d+)?)\s*%", re.I)
_DOWN = re.compile(r"\b(fell|dropped|slid|slumped|declined|lost)\s+(\d+(?:\.\d+)?)\s*%", re.I)


def extract_facts(title: str, body: str) -> tuple[Fact, ...]:
    text = f"{title}. {body}"
    facts: list[Fact] = []

    if m := _DIVIDEND.search(text):
        facts.append(Fact(type="dividend", value=m.group(1), unit="USD"))
    if _DOWNGRADE.search(text):
        facts.append(Fact(type="rating", value="downgrade"))
    elif _UPGRADE.search(text):
        facts.append(Fact(type="rating", value="upgrade"))
    if m := _DOWN.search(text):
        facts.append(Fact(type="price_move", value=f"-{m.group(2)}", unit="%"))
    elif m := _UP.search(text):
        facts.append(Fact(type="price_move", value=m.group(2), unit="%"))

    return tuple(facts)
