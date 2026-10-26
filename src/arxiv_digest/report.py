from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from .config import Config
from .models import Pick

ENV = Environment(
    loader=PackageLoader("arxiv_digest", "templates"),
    autoescape=select_autoescape(["html", "xml"]),
)


def author_line(pick: Pick) -> str:
    authors = pick.paper.authors
    text = ", ".join(authors[:3])
    return text + (f" 等 {len(authors)} 位作者" if len(authors) > 3 else "")


def institution_line(pick: Pick) -> str:
    if not pick.details or not pick.details.affiliations:
        return "暂未核实"
    # Collapse departments only when verified ranking evidence supplies a parent name.
    canonical = (
        {match.institution: match.rule for match in pick.ranking.matches} if pick.ranking else {}
    )
    names = list(
        dict.fromkeys(
            canonical.get(item.institution, item.institution) for item in pick.details.affiliations
        )
    )
    text = " · ".join(names[:2])
    return text + (f" 等 {len(names)} 个署名单位" if len(names) > 2 else "")


def render(config: Config, picks: list[Pick], now: datetime, stats: dict, *, demo=False):
    date = now.strftime("%Y-%m-%d")
    subject = f"{config.email.subject_prefix} · {date} · {len(picks)} 篇"
    authors = {pick.paper.id: author_line(pick) for pick in picks}
    institutions = {pick.paper.id: institution_line(pick) for pick in picks}
    html = ENV.get_template("digest.html").render(
        picks=picks,
        date=date,
        stats=stats,
        demo=demo,
        timezone=config.timezone,
        compact=config.compact_summaries,
        generated=now.strftime("%H:%M"),
        authors=authors,
        institutions=institutions,
    )
    lines = [subject, ""]
    if stats.get("announcement_dates"):
        lines += ["公告批次：" + ", ".join(stats["announcement_dates"]), ""]
    if demo:
        lines.extend(["离线演示：论文、分数和摘要均为虚构示例，未调用模型。", ""])
    for index, pick in enumerate(picks, 1):
        lines += [
            f"{index}. {pick.summary.title_zh}",
            pick.paper.title,
            "",
            pick.summary.tldr,
        ]
        if not config.compact_summaries:
            lines += [
                f"问题：{pick.summary.problem}",
                f"方法：{pick.summary.method}",
                f"结果：{pick.summary.results}",
                f"阅读提醒：{pick.summary.limitations}",
            ]
        lines += [
            "",
            "作者：" + authors[pick.paper.id],
            "机构：" + institutions[pick.paper.id],
            "首次提交：" + pick.paper.published[:10],
        ]
        if not demo:
            lines += ["论文：" + pick.paper.metadata_url, "PDF：" + pick.paper.metadata_pdf_url]
            lines.append(
                "项目：" + pick.details.project_url
                if pick.details and pick.details.project_url
                else "项目：暂未找到"
            )
        lines.append("")
    if not picks:
        lines.append("本次没有达到筛选阈值的新论文。")
    lines += ["", "AI 摘要仅依据论文标题与原始摘要生成，请以论文原文为准。"]
    return subject, "\n".join(lines), html


def write_report(
    directory: Path, config: Config, picks: list[Pick], now: datetime, stats: dict, *, demo=False
) -> tuple[str, str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    subject, text, html = render(config, picks, now, stats, demo=demo)
    (directory / "digest.html").write_text(html, encoding="utf-8")
    (directory / "digest.txt").write_text(text, encoding="utf-8")
    record = {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "demo": demo,
        "summary_basis": "title_and_abstract_only",
        "stats": stats,
        "papers": [pick.to_dict() for pick in picks],
        "metadata_completeness": {
            pick.paper.id: pick.details.completeness(pick.paper) for pick in picks if pick.details
        },
    }
    (directory / "digest.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return subject, text, html
