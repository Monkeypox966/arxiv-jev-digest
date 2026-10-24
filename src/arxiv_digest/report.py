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
STATUS_LABELS = {
    "unknown": "待核实",
    "found": "已核实",
    "not_found": "检查范围内未发现",
    "partial": "部分作者待核实",
    "unavailable": "来源暂不可用",
}


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
        ranking_enabled=config.ranking.enabled,
        institution_weight=config.ranking.institution_weight,
        min_institution_score=config.ranking.min_institution_score,
        generated=now.strftime("%H:%M"),
        status_labels=STATUS_LABELS,
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
    if config.ranking.enabled:
        lines += [
            f"排序权重：相关性 {1 - config.ranking.institution_weight:.0%} + "
            f"机构偏好 {config.ranking.institution_weight:.0%}；"
            f"机构最低分 {config.ranking.min_institution_score * 100:.0f}/100。",
            "机构分是可修改的偏好代理指标，不是对论文内容质量的评审。",
            f"机构过滤 {stats.get('institution_filtered', 0)} 篇；"
            f"机构待核实 {stats.get('institution_unknown', 0)} 篇；"
            f"机构未收录 {stats.get('institution_unlisted', 0)} 篇。",
            "未收录机构："
            + (
                "保留并标注，不加机构分。"
                if config.ranking.unlisted_policy == "keep"
                else "按配置直接过滤。"
            ),
            "",
        ]
    for index, pick in enumerate(picks, 1):
        lines += [
            f"{index}. {pick.summary.title_zh}",
            pick.paper.title,
            "作者：" + ", ".join(pick.paper.authors),
            "首次提交：" + pick.paper.published[:10],
            *(["公告批次：" + pick.paper.announced] if pick.paper.announced else []),
            *(["提取版本：" + pick.paper.version] if pick.paper.version else []),
            f"匹配分数：{pick.decision.score:.1%}",
        ]
        if pick.ranking:
            rank = pick.ranking
            lines.append(
                f"综合排序分：{rank.score * 100:.1f}/100；"
                f"机构偏好分：{rank.institution_score * 100:.0f}/100"
                if rank.status != "unknown"
                else f"综合排序分：{rank.score * 100:.1f}/100；机构待核实，未加分"
            )
            lines += [
                f"机构匹配：{match.institution} → {match.rule}（{match.score * 100:.0f}）"
                for match in rank.matches
            ]
            if rank.status == "unlisted":
                lines.append("已核实机构，但未命中偏好名单。")
        lines.append(f"一句话：{pick.summary.tldr}")
        if not config.compact_summaries:
            lines += [
                f"问题：{pick.summary.problem}",
                f"方法：{pick.summary.method}",
                f"结果：{pick.summary.results}",
                f"局限：{pick.summary.limitations}",
            ]
        if pick.details:
            coverage = pick.details.completeness(pick.paper)
            lines.append(
                f"机构核实覆盖：{coverage['covered_authors']}/{coverage['total_authors']} 位作者；"
                "项目状态：" + STATUS_LABELS[pick.details.project_status]
            )
            lines.append(
                "项目：" + (pick.details.project_url or "未能确认项目链接，详见来源与说明")
            )
            for affiliation in pick.details.affiliations:
                lines.append(
                    "机构：" + affiliation.institution + "；作者：" + ", ".join(affiliation.authors)
                )
                if affiliation.source:
                    lines.append("机构来源：" + affiliation.source)
            if pick.details.project_source:
                lines.append("项目链接来源：" + pick.details.project_source)
            if not pick.details.affiliations:
                lines.append("机构：未能核实，见论文原文")
            lines += ["核查来源：" + url for url in pick.details.sources]
            lines += ["说明：" + note for note in pick.details.notes]
        if not demo:
            lines += [pick.paper.metadata_url, pick.paper.metadata_pdf_url]
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
        "metadata_completeness": {
            pick.paper.id: pick.details.completeness(pick.paper) for pick in picks if pick.details
        },
    }
    (directory / "digest.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return subject, text, html
