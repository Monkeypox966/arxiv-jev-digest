"""Convert delimited LaTeX math to email-compatible Unicode text."""

from __future__ import annotations

import re

from pylatexenc.latex2text import LatexNodes2Text, MacroTextSpec, get_default_latex_context_db
from pylatexenc.latexwalker import LatexMacroNode, LatexWalker, LatexWalkerParseError
from pylatexenc.latexwalker import get_default_latex_context_db as get_parser_context
from pylatexenc.macrospec import MacroSpec

# Match complete math spans only. Ordinary prose, URLs and escaped dollars stay intact.
MATH = re.compile(
    r"(?<!\\)\$\$(?P<display>.+?)(?<!\\)\$\$"
    r"|(?<!\\)\\\[(?P<bracket>.+?)(?<!\\)\\\]"
    r"|(?<!\\)\\\((?P<paren>.+?)(?<!\\)\\\)"
    r"|(?<![\\$])\$(?![\s$])(?P<inline>[^\n]*?\S)(?<!\\)\$(?!\$)"
    r"|(?P<environment>(?<!\\)\\begin\{(?P<environment_name>"
    r"equation\*?|align\*?|gather\*?|displaymath)\}.*?\\end\{(?P=environment_name)\})",
    re.DOTALL,
)


def fraction(node, l2tobj):
    arguments = node.nodeargd.argnlist
    numerator = l2tobj.nodelist_to_text(getattr(arguments[0], "nodelist", [arguments[0]]))
    denominator = l2tobj.nodelist_to_text(getattr(arguments[1], "nodelist", [arguments[1]]))
    return f"({numerator})/({denominator})"


def math_text(text: str) -> str:
    """Keep unsupported/malformed math verbatim instead of silently dropping symbols."""
    context = get_default_latex_context_db()
    context.add_context_category(
        "email-fractions",
        macros=[MacroTextSpec(name, simplify_repl=fraction) for name in ("frac", "dfrac", "tfrac")],
        prepend=True,
    )
    converter = LatexNodes2Text(
        latex_context=context,
        strict_latex_spaces=True,
        keep_braced_groups=True,
        keep_braced_groups_minlen=0,
    )
    parser_context = get_parser_context()
    parser_context.add_context_category(
        "email-fractions",
        macros=[MacroSpec(name, "{{") for name in ("frac", "dfrac", "tfrac")],
        prepend=True,
    )

    def supported(nodes):
        for node in nodes:
            if node.isNodeType(LatexMacroNode):
                spec = context.get_macro_spec(node.macroname)
                # Never interpret document commands such as input, include or definitions.
                if not spec or not (
                    isinstance(spec.simplify_repl, str)
                    and spec.simplify_repl
                    or node.macroname
                    in {"frac", "dfrac", "tfrac", "sqrt", "text", "mathrm", "mathbf", "mathit"}
                ):
                    return False
            if getattr(node, "environmentname", None):
                return False
            if not supported(getattr(node, "nodelist", None) or []):
                return False
            args = getattr(node, "nodeargd", None)
            if args and not supported([arg for arg in args.argnlist if arg is not None]):
                return False
        return True

    def replace(match):
        expression = next(value for value in match.groupdict().values() if value is not None)
        try:
            nodes, _, _ = LatexWalker(
                expression, latex_context=parser_context, tolerant_parsing=False
            ).get_latex_nodes()
            if supported(nodes):
                return converter.nodelist_to_text(nodes)
        except (LatexWalkerParseError, ValueError, IndexError, RecursionError):
            pass
        return match.group()

    return MATH.sub(replace, text)
