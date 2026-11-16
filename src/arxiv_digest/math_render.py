"""Render LaTeX math as PNG images for HTML reports and related MIME email parts."""

from __future__ import annotations

import base64
import logging
from functools import lru_cache
from math import ceil

import resvg_py
import ziamath
from markupsafe import Markup, escape
from pylatexenc.latexwalker import LatexWalker

from .math_text import MATH

LOG = logging.getLogger(__name__)


@lru_cache(maxsize=256)
def formula_image(
    expression: str, display: bool, size: int, color: str
) -> tuple[bytes, float, float, float]:
    LatexWalker(expression, tolerant_parsing=False).get_latex_nodes()
    equation = ziamath.Latex(expression, size=size, color=color, inline=not display)
    # Unrecognized LaTeX commands become literal tokens in the parser. Preserve the
    # source instead of sending an apparently typeset but incorrect expression.
    if any("\\" in token for token in equation.mathml.itertext()):
        raise ValueError("Unsupported LaTeX command")
    svg = equation.svgxml()
    width = float(svg.attrib["width"])
    height = float(svg.attrib["height"])
    png = resvg_py.svg_to_bytes(
        svg_string=equation.svg(), zoom=2, background="white", skip_system_fonts=True
    )
    baseline = equation.node.bbox.ymin - equation.margin
    return png, width, min(baseline, 0), height


def math_html(text: str, size: int = 15, color: str = "#243832") -> Markup:
    """Escape prose and replace each complete formula with a self-contained PNG."""
    pieces = []
    position = 0
    for match in MATH.finditer(text):
        pieces.append(escape(text[position : match.start()]))
        expression = next(value for value in match.groupdict().values() if value is not None)
        display = match.group("display") is not None or match.group("bracket") is not None
        try:
            png, width, baseline, height = formula_image(expression, display, size, color)
            encoded = base64.b64encode(png).decode("ascii")
            style = "max-width:100%;height:auto;background:#fff;border:0;" + (
                "display:block;margin:10px 0;" if display else f"vertical-align:{baseline:.2f}px;"
            )
            pieces.append(
                Markup(
                    '<img src="data:image/png;base64,{}" alt="{}" '
                    'width="{}" height="{}" style="{}">'
                ).format(encoded, match.group(), ceil(width), ceil(height), style)
            )
        except Exception as exc:
            LOG.warning("Formula image unavailable (%s); retaining LaTeX text", type(exc).__name__)
            pieces.append(escape(match.group()))
        position = match.end()
    pieces.append(escape(text[position:]))
    return Markup("").join(pieces)
