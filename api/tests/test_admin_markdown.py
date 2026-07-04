"""Memory content renders as markdown, and hostile content renders inert."""

from __future__ import annotations

from app.admin.render import render_markdown


def test_renders_basic_markdown():
    html = str(render_markdown("**bold** and `code`"))
    assert "<strong>bold</strong>" in html
    assert "<code>code</code>" in html


def test_strips_script_and_handlers():
    hostile = "hi <script>alert(1)</script> <img src=x onerror=alert(2)> [x](javascript:alert(3))"
    html = str(render_markdown(hostile))
    assert "<script>" not in html
    assert "onerror" not in html
    assert "javascript:" not in html
