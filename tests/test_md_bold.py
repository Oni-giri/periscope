from __future__ import annotations

from periscope.web.context import md_bold


def test_md_bold_renders_strong_and_escapes_html() -> None:
    html = str(md_bold("Ship **GLM-5** and ignore <script>x</script>"))
    assert "<strong>GLM-5</strong>" in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_md_bold_empty_and_plain() -> None:
    assert str(md_bold(None)) == ""
    assert str(md_bold("plain")) == "plain"
    assert "<strong>" not in str(md_bold("no markers"))
