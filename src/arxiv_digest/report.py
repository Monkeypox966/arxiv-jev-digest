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


def render(config: Config, picks: list[Pick], now: datetime, stats: dict, *, demo=False):
    date = now.strftime("%Y-%m-%d")
    subject = f"{config.email.subject_prefix} · {date} · {len(picks)} 篇"
    labels = {i.id: i.label for i in config.interests}
    html = ENV.get_template("digest.html").render(
        picks=picks,
        date=date,
        stats=stats,
        labels=labels,
        demo=demo,
        timezone=config.timezone,
        compact=config.compact_summaries,
        generated=now.strftime("%H:%M"),
    )
    lines = [subject, "", "依据标题与原始摘要筛选、总结；未阅读论文全文。", ""]
    if stats.get("announcement_dates"):
        lines += [
            "最新公告批次：" + ", ".join(stats["announcement_dates"]),
            "来源：https://arxiv.org/list/" + config.arxiv.categories[0] + "/recent",
            "",
        ]
    if demo:
        lines.extend(["离线演示：论文、分数和摘要均为虚构示例，未调用任何模型。", ""])
    for index, pick in enumerate(picks, 1):
        lines += [
            f"{index}. {pick.summary.title_zh}",
            pick.paper.title,
            "作者：" + ", ".join(pick.paper.authors),
            "首次提交：" + pick.paper.published[:10],
            *(["公告批次：" + pick.paper.announced] if pick.paper.announced else []),
            f"匹配分数：{pick.decision.score:.1%}",
            f"一句话：{pick.summary.tldr}",
            f"结果：{pick.summary.results}",
        ]
        if not config.compact_summaries:
            lines += [
                f"问题：{pick.summary.problem}",
                f"方法：{pick.summary.method}",
                f"局限：{pick.summary.limitations}",
            ]
        if pick.details:
            lines.append("项目：" + (pick.details.project_url or "未找到作者项目网站"))
            for affiliation in pick.details.affiliations:
                lines.append(
                    "机构：" + affiliation.institution + "；作者：" + ", ".join(affiliation.authors)
                )
            if not pick.details.affiliations:
                lines.append("机构：未能核实，见论文原文")
            lines += ["核查来源：" + url for url in pick.details.sources]
            lines += ["说明：" + note for note in pick.details.notes]
        if not demo:
            lines += [pick.paper.url, pick.paper.pdf_url]
        lines.append("")
    if not picks:
        lines.append("本次没有达到筛选阈值的新论文。")
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
    }
    (directory / "digest.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return subject, text, html
