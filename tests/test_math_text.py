import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from arxiv_digest.math_text import math_text
from arxiv_digest.models import Decision, Pick
from arxiv_digest.report import write_report


@pytest.mark.parametrize(
    "source,expected",
    [
        (r"$\nabla$Mimic", "∇Mimic"),
        (r"快 $11.5\times$", "快 11.5×"),
        (r"\(\alpha \leq \beta\)", "α≤β"),
        (r"$$\nabla f(x)$$", "∇f(x)"),
        (r"\[\sqrt{x}\]", "√(x)"),
        (r"$x_{t+1}=\frac{a+b}{c+d}$", "x_{t+1}=(a+b)/(c+d)"),
        (r"$\frac12$", "(1)/(2)"),
        (r"$\dfrac{a+b}{c+d}$", "(a+b)/(c+d)"),
        (r"$\unknown{x}$", r"$\unknown{x}$"),
        (r"$\input{/etc/passwd}$", r"$\input{/etc/passwd}$"),
        (r"$\frac{a}{b$", r"$\frac{a}{b$"),
        (r"Cost $5 and $10, 42% https://x/a_b", r"Cost $5 and $10, 42% https://x/a_b"),
        (r"escaped \$5 and unmatched $x", r"escaped \$5 and unmatched $x"),
    ],
)
def test_math_text_preserves_meaning_and_falls_back_without_losing_unknown_commands(
    source, expected
):
    assert math_text(source) == expected


def test_nablamimic_titles_and_abstract_render_from_cached_latex_without_changing_source(
    config, paper, summary, tmp_path
):
    paper = replace(
        paper,
        title=r"$\nabla$Mimic: Ultra-Efficient Humanoid Control with Differentiable Simulation",
    )
    summary.title_zh = r"$\nabla$Mimic：基于可微模拟的超高效人形控制"
    summary.tldr = r"$\nabla$Mimic 的伴随计算快 $11.5\times$。" + "\n\n保留 <script> 和 42%。"
    pick = Pick(
        paper,
        Decision(score=0.9, interests={}, exclusions={}, excluded=False, model="test"),
        summary,
    )
    _, text, html = write_report(tmp_path, config, [pick], datetime.now(UTC), {"fetched": 1})
    assert "∇Mimic: Ultra-Efficient" in text
    assert "∇Mimic：基于可微模拟" in text
    assert "∇Mimic 的伴随计算快 11.5×。" in text
    assert r"\nabla" not in text
    assert html.count('src="data:image/png;base64,') == 4
    assert "<script>" not in html and "&lt;script&gt;" in html
    record = json.loads((tmp_path / "digest.json").read_text("utf-8"))
    assert record["papers"][0]["paper"]["title"] == paper.title
    assert paper.title.startswith(r"$\nabla$")
