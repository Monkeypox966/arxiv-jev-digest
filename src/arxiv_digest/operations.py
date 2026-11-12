"""Bounded metadata diagnostics and run summaries. Only paper digests may send email."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .arxiv import fetch_by_ids
from .clients import DeepSeekClient
from .config import Config, require_env
from .enrichment import Enricher
from .http import ServiceError
from .institution_assessment import InstitutionAssessor
from .mailer import SMTPMailer
from .store import Store
from .usage import UsageTracker


def persist_outbox(project_root: Path) -> None:
    script = project_root / ".github/scripts/state.py"
    if not script.is_file() or not os.getenv("GH_TOKEN"):
        raise ServiceError(
            "Pending delivery cannot be persisted; email delivery is blocked", fatal=True
        )
    try:
        result = subprocess.run(
            [sys.executable, str(script), "save"],
            cwd=project_root,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ServiceError(
            "Pending delivery persistence failed; email delivery is blocked", fatal=True
        ) from None
    if result.returncode:
        raise ServiceError(
            "Pending delivery persistence failed; email delivery is blocked", fatal=True
        )


def assess_institutions(config: Config, client: httpx.Client, names: list[str]) -> dict:
    """Explicit institution review without paper fetching, JEV, SMTP or delivery history updates."""
    names = list(dict.fromkeys(name.strip() for name in names if name.strip()))
    if not 1 <= len(names) <= 5:
        raise ValueError("Assess requires 1 to 5 nonempty institution names")
    now = datetime.now(ZoneInfo(config.timezone))
    directory = config.output_dir / (
        now.strftime("%Y%m%d-%H%M%S-institutions-") + uuid.uuid4().hex[:8]
    )
    directory.mkdir(parents=True, exist_ok=True)
    usage = UsageTracker(config.budget)
    health = {"schema_version": 1, "operation": "institutions", "started_at": now.isoformat()}
    try:
        model = DeepSeekClient(config, client, usage=usage)
        with Store(config.state_dir) as store:
            assessor = InstitutionAssessor(config, client, model, store, now=now)
            results = [assessor.assess(name).model_dump() for name in names]
            registry = store.export_institutions()
        usage.assert_within_limits()
        (directory / "institutions.json").write_text(
            json.dumps(registry, ensure_ascii=False, indent=2), "utf-8"
        )
        (directory / "institution-review.json").write_text(
            json.dumps(
                {"results": results, "usage": usage.snapshot()}, ensure_ascii=False, indent=2
            ),
            "utf-8",
        )
        result = {
            "institutions_checked": len(results),
            "learned_total": len(registry["institutions"]),
            "emails_sent": 0,
            "output_dir": str(directory),
            "usage": usage.snapshot(),
        }
        health.update(status="success", stats=result)
        return result
    except BaseException as exc:
        health.update(
            status="failed",
            error_type=type(exc).__name__,
            error=str(exc) if isinstance(exc, ServiceError) else type(exc).__name__,
        )
        raise
    finally:
        health["finished_at"] = datetime.now(ZoneInfo(config.timezone)).isoformat()
        health["usage"] = usage.snapshot()
        for name, value in (("health.json", health), ("usage.json", usage.snapshot())):
            (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")


def inspect_metadata(
    config: Config, client: httpx.Client, ids: list[str], *, refresh=False
) -> dict:
    if not 1 <= len(ids) <= 5:
        raise ValueError("Inspect requires 1 to 5 arXiv IDs")
    now = datetime.now(ZoneInfo(config.timezone))
    directory = config.output_dir / (now.strftime("%Y%m%d-%H%M%S-inspect-") + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=True)
    usage = UsageTracker(config.budget)
    health = {"schema_version": 1, "operation": "inspect", "started_at": now.isoformat()}
    try:
        require_env("DEEPSEEK_API_KEY")
        papers = fetch_by_ids(config.arxiv, client, ids)
        enricher = Enricher(config, client, DeepSeekClient(config, client, usage=usage))
        results = []
        with Store(config.state_dir) as store:
            for paper in papers:
                key = enricher.key(paper)
                cached = None if refresh else enricher.cached_details(store, paper, now=now)
                details = cached if cached is not None else enricher.enrich(paper)
                if not cached:
                    store.cache("details", key, details.model_dump(), now=now)
                results.append(
                    {
                        "paper": paper.to_dict(),
                        "details": details.model_dump(),
                        "completeness": details.completeness(paper),
                        "cached": bool(cached),
                    }
                )
        usage.assert_within_limits()
        record = {
            "schema_version": 2,
            "affiliation_basis": "paper_institutions",
            "operation": "inspect",
            "papers": results,
            "usage": usage.snapshot(),
            "emails_sent": 0,
        }
        (directory / "metadata.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )
        health.update(status="success", stats={"inspected": len(papers), "emails_sent": 0})
        return {
            "inspected": len(papers),
            "emails_sent": 0,
            "output_dir": str(directory),
            "usage": usage.snapshot(),
        }
    except BaseException as exc:
        health.update(
            status="failed",
            error_type=type(exc).__name__,
            error=str(exc) if isinstance(exc, ServiceError) else type(exc).__name__,
        )
        raise
    finally:
        health["usage"] = usage.snapshot()
        health["finished_at"] = datetime.now(ZoneInfo(config.timezone)).isoformat()
        for name, value in (("health.json", health), ("usage.json", usage.snapshot())):
            (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")


def latest_health(config: Config) -> dict:
    paths = list(config.output_dir.glob("*/health.json"))
    if not paths:
        return {"status": "unavailable", "error": "No operation health report was produced"}
    return json.loads(max(paths, key=lambda path: path.stat().st_mtime_ns).read_text("utf-8"))


def test_delivery(config: Config, *, outbox_hook=None, mailer_factory=SMTPMailer) -> dict:
    """Compatibility entry point: non-paper test emails are disabled."""
    return {"emails_sent": 0, "test_delivery": True, "skip_reason": "non_paper_email_disabled"}


def actions_summary(config: Config) -> dict:
    health = latest_health(config)
    stats = health.get("stats", {})
    usage = health.get("usage", {}).get("totals", {})
    lines = [
        "## arXiv 日报运行报告",
        "",
        f"状态：**{health.get('status', 'unknown')}**",
        "",
        "| 项目 | 数量 |",
        "| --- | ---: |",
    ]
    for label, value in (
        ("arXiv 候选", stats.get("fetched", stats.get("inspected", 0))),
        ("入选论文", stats.get("selected_unique", 0)),
        ("已发送日报", stats.get("emails_sent", 0)),
        ("尚未提取署名机构", stats.get("metadata_missing", 0)),
        ("机构清单尚未完成 PDF 核查", stats.get("metadata_partial", 0)),
        ("待核查论文重试", stats.get("metadata_retries", 0)),
        ("机构补评候选重试", stats.get("institution_assessment_retries", 0)),
        ("本轮暂缓评估机构", stats.get("institution_deferred", 0)),
        ("JEV API 尝试", usage.get("jev_calls", 0)),
        ("DeepSeek API 尝试", usage.get("deepseek_calls", 0)),
        ("服务报告输入 token", usage.get("input_tokens", 0)),
        ("服务报告输出 token", usage.get("output_tokens", 0)),
    ):
        lines.append(f"| {label} | {value} |")
    if not health.get("usage", {}).get("usage_complete", True):
        lines += ["", "部分服务未返回完整 token 用量；预算仍保留预估值，未按零消耗处理。"]
    if stats.get("scheduled_for"):
        lines += [
            "",
            "计划发送时间：" + stats["scheduled_for"],
            f"程序开始处理时已延迟：{stats['start_delay_seconds']} 秒。",
            f"当天已送达而跳过的收件人数：{stats.get('skipped_recipients', 0)}。",
        ]
    if stats.get("skip_reason"):
        lines += [
            "",
            "跳过原因："
            + {
                "before_daily_send_time": "尚未到当天发送时间；不抓取、不调用模型、不发邮件。",
                "already_delivered_today": "当天日报已经发送；不抓取、不调用模型、不重复发信。",
                "waiting_for_arxiv_update": (
                    "arXiv 仍是旧公告批次，等待更新；不调用模型、不发邮件，下次继续检查。"
                ),
                "no_matching_papers": "没有新的匹配论文；不发送空日报。",
            }.get(stats["skip_reason"], stats["skip_reason"]),
        ]
    if health.get("error"):
        lines += ["", "错误：" + health["error"].replace("\n", " ")]
    if health.get("recovery_required"):
        lines += ["", "公告检查点超出可核实范围，需要按操作手册显式恢复；未自动推进检查点。"]
    target = os.getenv("GITHUB_STEP_SUMMARY")
    if target:
        with Path(target).open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
    return health


def notify_failure(config: Config, run_url: str, *, mailer_factory=SMTPMailer) -> int:
    """Compatibility entry point: failure emails are disabled, including legacy configs."""
    return 0
