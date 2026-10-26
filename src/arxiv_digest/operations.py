"""Bounded metadata diagnostics, run summaries, and optional failure mail."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from .arxiv import fetch_by_ids
from .clients import DeepSeekClient
from .config import Config, recipients, require_env
from .enrichment import Enricher
from .http import ServiceError
from .mailer import SMTPMailer, build_message
from .store import Store, recipient_key
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
    """Explicit SMTP test: no model calls and no real-paper history mutation."""
    addresses = recipients(config)
    if not addresses:
        raise ValueError("Delivery test requires EMAIL_TO")
    now = datetime.now(ZoneInfo(config.timezone))
    directory = config.output_dir / (
        now.strftime("%Y%m%d-%H%M%S-delivery-test-") + uuid.uuid4().hex[:8]
    )
    directory.mkdir(parents=True, exist_ok=True)
    usage = UsageTracker(config.budget)
    health = {"schema_version": 1, "operation": "delivery_test", "started_at": now.isoformat()}
    sent = 0
    try:
        with Store(config.state_dir) as store:
            for address in addresses:
                key = recipient_key(address)
                if store.pending(key):
                    raise ServiceError("Resolve pending deliveries before running an SMTP test")
                with mailer_factory(config) as mailer:
                    delivery = store.prepare(key, "delivery-test-v1", [], [])
                    if outbox_hook:
                        outbox_hook()
                    subject = config.email.subject_prefix + " · 投递联调测试"
                    text = (
                        "这是一封投递联调测试邮件，用于验证 QQ SMTP、发送前状态保存和确认流程。\n"
                        "本次没有调用论文模型，也没有清除或改写真实论文的发送历史。\n"
                        "测试时间：" + now.isoformat() + "\n"
                    )
                    import html

                    mailer.send(
                        build_message(
                            config,
                            address,
                            delivery,
                            subject,
                            text,
                            "<pre>" + html.escape(text) + "</pre>",
                            now,
                        )
                    )
                    store.resolve(delivery, "sent")
                    sent += 1
        result = {"emails_sent": sent, "test_delivery": True, "output_dir": str(directory)}
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
        health["usage"] = usage.snapshot()
        health["finished_at"] = datetime.now(ZoneInfo(config.timezone)).isoformat()
        for name, value in (("health.json", health), ("usage.json", usage.snapshot())):
            (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")


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
        ("待核查论文重试", stats.get("metadata_retries", 0)),
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
    if not config.notifications.email_on_failure:
        return 0
    parts = urlsplit(run_url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("Failure notification requires a public HTTPS run URL")
    addresses = recipients(config)
    if not addresses:
        raise ValueError("Failure notification requires EMAIL_TO")
    now = datetime.now(ZoneInfo(config.timezone))
    health = latest_health(config)
    subject = config.email.subject_prefix + " · 运行异常"
    text = (
        "日报任务未完整完成，请查看 GitHub Actions 运行记录：\n" + run_url + "\n\n"
        "状态：" + health.get("status", "unavailable") + "\n"
        "原因：" + health.get("error", "工作流的准备或状态保存步骤失败") + "\n\n"
        "部分缓存可能已保存。邮件可能已被服务器接受，请先检查收件箱及 pending 状态，"
        "再决定是否重试；不要直接重复发送。\n"
    )
    import html

    with mailer_factory(config) as mailer:
        for address in addresses:
            mailer.send(
                build_message(
                    config,
                    address,
                    "failure-" + uuid.uuid4().hex,
                    subject,
                    text,
                    "<pre>" + html.escape(text) + "</pre>",
                    now,
                )
            )
    return len(addresses)
