"""Render agent-written memory content as sanitized markdown for the console.

Memory content is free text written by agents; rendering it as HTML is a
stored-XSS surface. We convert markdown then run the output through ``nh3``
(ammonia) with a conservative allow-list — no raw HTML, scripts, event
handlers, or ``javascript:`` URLs survive. See SECURITY.md.
"""

from __future__ import annotations

import markdown as _markdown
import nh3
from markupsafe import Markup

_ALLOWED_TAGS = {
    "p",
    "br",
    "hr",
    "pre",
    "code",
    "blockquote",
    "strong",
    "em",
    "b",
    "i",
    "del",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "a",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
}
_ALLOWED_ATTRS = {"a": {"href", "title"}}


def render_markdown(text: str) -> Markup:
    raw_html = _markdown.markdown(text or "", extensions=["fenced_code", "tables"])
    clean = nh3.clean(
        raw_html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        url_schemes={"http", "https", "mailto"},
    )
    return Markup(clean)
