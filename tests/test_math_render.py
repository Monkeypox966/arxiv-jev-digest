import base64
import re
import struct
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser

import pytest
from bs4 import BeautifulSoup

from arxiv_digest.mailer import build_message
from arxiv_digest.math_render import formula_image, math_html


@pytest.mark.parametrize(
    "expression",
    [
        r"\nabla",
        r"\frac{a+b}{c+d}",
        r"x_{t+1}^{2} = \sqrt{\alpha+\beta}",
        r"\sum_{i=1}^{n} \gamma^i r_i",
        r"\int_0^1 f(x)\,dx",
        r"\begin{bmatrix}1&2\\3&4\end{bmatrix}",
        r"\begin{cases}x^2&x>0\\-x&x\leq 0\end{cases}",
        r"\begin{aligned}a&=b+c\\d&=e-f\end{aligned}",
    ],
)
def test_math_is_typeset_into_a_real_retina_png(expression):
    png, width, _, height = formula_image(expression, True, 18, "#243832")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    pixel_width, pixel_height = struct.unpack(">II", png[16:24])
    assert abs(pixel_width - width * 2) <= 1
    assert abs(pixel_height - height * 2) <= 1
    assert len(png) > 500


def test_rendered_math_is_embedded_in_the_mime_message_once_per_image(config):
    html = str(math_html(r"摘要 $\frac{a}{b}$，重复 $\frac{a}{b}$。"))
    message = build_message(
        config, "reader@example.com", "math-proof", "日报", "文本 a/b", html, datetime.now(UTC)
    )
    parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
    body = parsed.get_body(preferencelist=("html",)).get_content()
    sources = re.findall(r'src="cid:([^\"]+)"', body)
    assert len(sources) == 2 and sources[0] == sources[1]
    assert "data:image" not in body
    images = [part for part in parsed.walk() if part.get_content_type() == "image/png"]
    assert len(images) == 1
    assert images[0]["Content-ID"] == f"<{sources[0]}>"
    assert images[0].get_content_disposition() == "inline"
    assert images[0].get_payload(decode=True).startswith(b"\x89PNG")
    assert parsed.get_body(preferencelist=("plain",)).get_content().strip() == "文本 a/b"
    assert any(part.get_content_type() == "multipart/related" for part in parsed.walk())


def test_formula_html_escapes_prose_and_keeps_unsupported_source_visible():
    source = r"<script>alert(1)</script> $\unknown{x}$ and $\frac{a}{b$"
    html = str(math_html(source))
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert BeautifulSoup(html, "html.parser").get_text() == source


def test_display_math_and_offline_html_preserve_formula_alt_text():
    html = str(math_html(r"行内 $x^2$，块公式 \[\frac{a}{b}\]。"))
    images = BeautifulSoup(html, "html.parser").find_all("img")
    assert len(images) == 2
    assert images[0]["alt"] == "$x^2$"
    assert images[1]["alt"] == r"\[\frac{a}{b}\]"
    assert "display:block" in images[1]["style"]
    assert base64.b64decode(images[0]["src"].split(",")[1]).startswith(b"\x89PNG")
