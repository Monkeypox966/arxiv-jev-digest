from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, time
from zoneinfo import ZoneInfo

import httpx

from .arxiv import category_matches, fetch_papers
from .clients import (
    DeepSeekClient,
    JevClient,
    decision_key,
    fingerprint,
    profile_key,
    summary_key,
)
from .config import Config, recipients, validate_credentials
from .enrichment import Enricher
from .http import ServiceError
from .mailer import SMTPMailer, build_message
from .models import Decision, Paper, Pick, Summary
from .ranking import passes_institution_filter, rank_paper
from .report import write_report
from .store import Store, recipient_key
from .usage import UsageTracker

LOG = logging.getLogger(__name__)


def bounded_map(fn: Callable, values: Iterable, workers: int):
    """At most `workers` requests in flight, including when a fatal error occurs."""
    iterator = iter(values)
    executor = ThreadPoolExecutor(max_workers=workers)
    pending = {}

    def submit_next():
        try:
            value = next(iterator)
        except StopIteration:
            return
        pending[executor.submit(fn, value)] = value

    try:
        for _ in range(workers):
            submit_next()
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            completed = []
            error = None
            for future in done:
                value = pending.pop(future)
                try:
                    completed.append((value, future.result()))
                except Exception as exc:
                    error = exc
            # Persist completed results before scheduling more paid calls.
            yield from completed
            if error:
                raise error
            for _ in completed:
                submit_next()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def run(
    config: Config,
    client: httpx.Client,
    *,
    dry_run: bool = False,
    mailer_factory=SMTPMailer,
    now: datetime | None = None,
    outbox_hook: Callable[[], None] | None = None,
    daily_after: time | None = None,
) -> dict:
    now = (now or datetime.now(ZoneInfo(config.timezone))).astimezone(ZoneInfo(config.timezone))
    run_dir = config.output_dir / (now.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    run_dir.mkdir(parents=True, exist_ok=True)
    usage = UsageTracker(config.budget)
    health = {"schema_version": 1, "started_at": now.isoformat(), "status": "running"}
    try:
        if dry_run and daily_after is not None:
            raise ValueError("Daily delivery guard cannot be used with a dry run")
        result = _run(
            config,
            client,
            dry_run=dry_run,
            mailer_factory=mailer_factory,
            now=now,
            run_dir=run_dir,
            usage=usage,
            outbox_hook=outbox_hook,
            daily_after=daily_after,
        )
        health.update(status="success", stats=result)
        health["warnings"] = [
            label
            for key, label in (
                ("metadata_missing", "部分候选尚未提取到有来源的署名机构，报告已标注。"),
                ("metadata_unavailable", "部分元数据来源暂不可用，不等同于论文没有机构或项目。"),
            )
            if result.get(key)
        ]
        return result
    except BaseException as exc:
        health.update(
            status="cancelled" if isinstance(exc, KeyboardInterrupt) else "failed",
            error_type=type(exc).__name__,
            error=str(exc) if isinstance(exc, ServiceError) else type(exc).__name__,
            recovery_required=bool(getattr(exc, "recovery_required", False)),
        )
        raise
    finally:
        health["finished_at"] = datetime.now(ZoneInfo(config.timezone)).isoformat()
        health["usage"] = usage.snapshot()
        for name, data in (("health.json", health), ("usage.json", usage.snapshot())):
            (run_dir / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def _run(
    config: Config,
    client: httpx.Client,
    *,
    dry_run: bool,
    mailer_factory,
    now: datetime,
    run_dir,
    usage: UsageTracker,
    outbox_hook: Callable[[], None] | None = None,
    daily_after: time | None = None,
) -> dict:
    addresses = recipients(config)
    # A dry run without configured recipients uses a synthetic preview identity.
    targets = addresses or ["preview@example.invalid"]
    profile = profile_key(config)
    daily_stats = {}
    if daily_after is not None:
        if not addresses:
            raise ValueError("Daily delivery guard requires configured recipients")
        due = datetime.combine(now.date(), daily_after, tzinfo=now.tzinfo)
        daily_stats = {
            "daily_delivery": True,
            "scheduled_for": due.isoformat(),
            "start_delay_seconds": max(0, int((now - due).total_seconds())),
            "skipped_recipients": 0,
        }
        if now < due:
            LOG.info("Daily delivery is not due until %s", due.isoformat())
            return {
                **daily_stats,
                "skip_reason": "before_daily_send_time",
                "emails_sent": 0,
                "selected_unique": 0,
                "usage": usage.snapshot(),
            }
    with Store(config.state_dir) as store:
        store.prune(config.retention.cache_days, now=now)
        if not dry_run:
            for address in targets:
                pending = store.pending(recipient_key(address))
                if pending:
                    raise ServiceError(
                        f"Unresolved email attempt {pending[0]['id']}; "
                        "check inbox, then use `pending` and `resolve`"
                    )
        if daily_after is not None:
            targets = [
                address
                for address in targets
                if not store.delivered_today(recipient_key(address), now=now)
            ]
            daily_stats["skipped_recipients"] = len(addresses) - len(targets)
            if not targets:
                LOG.info("Daily digest already delivered to all recipients; no APIs or SMTP used")
                return {
                    **daily_stats,
                    "skip_reason": "already_delivered_today",
                    "emails_sent": 0,
                    "selected_unique": 0,
                    "usage": usage.snapshot(),
                }
        validate_credentials(config, send=not dry_run)
        catch_up = config.arxiv.source == "latest" and config.arxiv.catch_up
        source_keys = (
            {
                category: fingerprint(
                    {
                        "categories": [category],
                        "cross_lists": config.arxiv.include_cross_lists,
                        "mode": "latest",
                    }
                )
                for category in config.arxiv.categories
            }
            if catch_up
            else {}
        )
        checkpoints = {
            (address, category): store.checkpoint(recipient_key(address), source_key)
            for address in targets
            for category, source_key in source_keys.items()
        }
        groups = {}
        if catch_up:
            for category in config.arxiv.categories:
                dates = [
                    checkpoints[address, category]
                    for address in targets
                    if checkpoints[address, category]
                ]
                settings = config.arxiv.model_copy(update={"categories": [category]})
                groups[category] = (
                    fetch_papers(settings, client, now=now, since=min(dates))
                    if dates
                    else fetch_papers(settings, client, now=now)
                )
        else:
            groups[""] = fetch_papers(config.arxiv, client, now=now)
        latest_by_group = {
            category: max((p.announced for p in group if p.announced), default="")
            for category, group in groups.items()
        }
        updates = [
            (source_keys[category], date)
            for category, date in latest_by_group.items()
            if category in source_keys and date
        ]
        papers = {}
        for group in groups.values():
            for paper in group:
                if paper.id not in papers or paper.announced > papers[paper.id].announced:
                    papers[paper.id] = paper
        fetched = list(papers.values())
        LOG.info("Fetched %d unique arXiv candidate papers", len(fetched))
        pools, retried = {}, set()
        for address in targets:
            pool = {}
            for category, group in groups.items():
                checkpoint = checkpoints.get((address, category))
                for paper in group:
                    if (
                        not catch_up
                        or not paper.announced
                        or paper.announced == latest_by_group[category]
                        or (checkpoint and paper.announced > checkpoint)
                    ) and store.eligible(recipient_key(address), profile, paper.id, now=now):
                        pool[paper.id] = papers[paper.id]
            if config.enrichment.enabled and config.enrichment.retry_missing_affiliations:
                for raw in store.due_metadata(
                    recipient_key(address),
                    profile,
                    now=now,
                    limit=config.enrichment.retry_limit_per_run,
                ):
                    paper = papers.get(raw["id"]) or Paper(**raw)
                    categories = (
                        paper.categories
                        if config.arxiv.include_cross_lists
                        else paper.categories[:1]
                    )
                    if not any(
                        category_matches(category, allowed)
                        for category in categories
                        for allowed in config.arxiv.categories
                    ):
                        continue
                    pool[paper.id] = paper
                    papers[paper.id] = paper
                    retried.add(paper.id)
            pools[address] = list(pool.values())
        wanted = {p.id: p for pool in pools.values() for p in pool}
        decisions = {}
        uncached = []
        for paper in wanted.values():
            cached = store.cached("decision", decision_key(config, paper))
            if cached:
                decisions[paper.id] = Decision.model_validate(cached)
            else:
                uncached.append(paper)
        LOG.info("Screening %d papers with Jev (%d cached)", len(uncached), len(decisions))
        jev = JevClient(config, client, usage=usage)
        for completed, (paper, decision) in enumerate(
            bounded_map(jev.judge, uncached, config.jev.concurrency), 1
        ):
            decisions[paper.id] = decision
            store.cache("decision", decision_key(config, paper), decision.model_dump(), now=now)
            if completed % 100 == 0 or completed == len(uncached):
                LOG.info("Jev screening progress: %d/%d", completed, len(uncached))
        qualified = {
            address: [
                p
                for p in pool
                if not decisions[p.id].excluded
                and decisions[p.id].score >= config.selection.min_score
            ]
            for address, pool in pools.items()
        }
        qualified_papers = {p.id: p for group in qualified.values() for p in group}
        deepseek = DeepSeekClient(config, client, usage=usage)
        details = {}
        detail_calls = 0

        def enrich_papers(group):
            nonlocal detail_calls
            if not config.enrichment.enabled or not group:
                return
            enricher = Enricher(config, client, deepseek)
            for index, paper in enumerate(group.values(), 1):
                key = enricher.key(paper)
                cached = enricher.cached_details(store, paper, now=now)
                detail = cached if cached is not None else enricher.enrich(paper)
                details[paper.id] = detail
                if not cached:
                    detail_calls += 1
                    # Cache temporary failures briefly too; they must not cause
                    # repeated paid extraction within the same retry interval.
                    store.cache("details", key, detail.model_dump(), now=now)
                if index % 5 == 0 or index == len(group):
                    LOG.info("Affiliation metadata progress: %d/%d", index, len(group))

        rankings = {}
        if config.ranking.enabled:
            # Every relevant candidate gets the same opportunity for affiliation
            # verification. Enriching only the old Top K would bias selection.
            enrich_papers(qualified_papers)
            rankings = {
                p.id: rank_paper(p, decisions[p.id].score, details.get(p.id), config.ranking)
                for p in qualified_papers.values()
            }
        selected = {}
        for address, pool in qualified.items():
            ranked = sorted(
                (
                    p
                    for p in pool
                    if not config.ranking.enabled
                    or passes_institution_filter(rankings[p.id], config.ranking)
                ),
                key=lambda p: (
                    -(rankings[p.id].score if rankings else decisions[p.id].score),
                    -decisions[p.id].score,
                    p.id,
                ),
            )
            selected[address] = ranked[: config.selection.top_k]
        summary_papers = {p.id: p for group in selected.values() for p in group}
        if config.ranking.enabled:
            run_dir.mkdir(parents=True, exist_ok=True)
            audit = {
                "institution_weight": config.ranking.institution_weight,
                "min_institution_score": config.ranking.min_institution_score,
                "unknown_policy": config.ranking.unknown_policy,
                "unlisted_policy": config.ranking.unlisted_policy,
                "papers": [
                    {
                        "id": p.id,
                        "title": p.title,
                        "url": p.url,
                        "details": details[p.id].model_dump() if p.id in details else None,
                        "ranking": rankings[p.id].model_dump(),
                        "passes_institution_filter": passes_institution_filter(
                            rankings[p.id], config.ranking
                        ),
                        "selected_for_any_recipient": p.id in summary_papers,
                    }
                    for p in qualified_papers.values()
                ],
            }
            (run_dir / "ranking-audit.json").write_text(
                json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        else:
            enrich_papers(summary_papers)
        summaries = {}
        unsummarized = []
        for paper in summary_papers.values():
            cached = store.cached("summary", summary_key(config, paper))
            if cached:
                summaries[paper.id] = Summary.model_validate(cached)
            else:
                unsummarized.append(paper)
        LOG.info("Summarizing %d selected papers (%d cached)", len(unsummarized), len(summaries))
        for completed, (paper, summary) in enumerate(
            bounded_map(deepseek.summarize, unsummarized, config.deepseek.concurrency), 1
        ):
            summaries[paper.id] = summary
            store.cache("summary", summary_key(config, paper), summary.model_dump(), now=now)
            if completed % 5 == 0 or completed == len(unsummarized):
                LOG.info("DeepSeek summary progress: %d/%d", completed, len(unsummarized))
        stats = {
            **daily_stats,
            "fetched": len(fetched),
            "metadata_retries": len(retried),
            "new_candidates": len(wanted),
            "jev_calls": len(uncached),
            "summary_calls": len(unsummarized),
            "relevance_qualified": len(qualified_papers),
            "metadata_lookups": detail_calls,
            "institution_filtered": sum(
                not passes_institution_filter(r, config.ranking) for r in rankings.values()
            ),
            "institution_unknown": sum(r.status == "unknown" for r in rankings.values()),
            "institution_unlisted": sum(r.status == "unlisted" for r in rankings.values()),
            "metadata_missing": sum(not d.verified_affiliations() for d in details.values()),
            "metadata_unavailable": sum(bool(d.failed_sources) for d in details.values()),
            "selected_unique": len(summary_papers),
            "emails_sent": 0,
            "dry_run": dry_run,
            "announcement_dates": sorted({p.announced for p in fetched if p.announced}),
            "usage": usage.snapshot(),
        }
        usage.assert_within_limits()
        for index, address in enumerate(targets, 1):
            picks = [
                Pick(p, decisions[p.id], summaries[p.id], details.get(p.id), rankings.get(p.id))
                for p in selected[address]
            ]
            directory = run_dir if len(targets) == 1 else run_dir / f"digest-{index}"
            subject, text, html = write_report(
                directory,
                config,
                picks,
                now,
                {k: v for k, v in stats.items() if k != "emails_sent"},
            )
            LOG.info("Saved %d-paper report to %s", len(picks), directory)
            if dry_run:
                continue
            key = recipient_key(address)
            if config.enrichment.enabled and config.enrichment.retry_missing_affiliations:
                chosen = {p.id for p in selected[address]}
                for paper in qualified[address]:
                    detail = details.get(paper.id)
                    if detail and not detail.verified_affiliations():
                        if paper.id not in chosen:
                            store.queue_metadata_retry(
                                key,
                                profile,
                                paper.to_dict(),
                                now=now,
                                delay_hours=config.enrichment.incomplete_cache_hours,
                                max_attempts=config.enrichment.retry_attempts,
                            )
                    else:
                        store.clear_metadata_retry(key, None, paper.id)
            candidates = [p.id for p in pools[address]]
            if not picks and not config.email.send_empty:
                store.mark_reviewed(key, profile, candidates, checkpoint_updates=updates)
                LOG.info("No new matches for recipient %d; email skipped", index)
                continue
            # Authenticate before preparing an outbox entry: a failed login cannot send mail.
            with mailer_factory(config) as mailer:
                usage.assert_within_limits()
                delivery = store.prepare(
                    key,
                    profile,
                    candidates,
                    [p.paper.id for p in picks],
                    checkpoint_updates=updates,
                )
                if outbox_hook:
                    # Persist the pending record remotely before SMTP DATA. An
                    # abruptly lost runner must not lose the duplicate-send guard.
                    outbox_hook()
                message = build_message(config, address, delivery, subject, text, html, now)
                mailer.send(message)
                store.resolve(delivery, "sent")
                stats["emails_sent"] += 1
                LOG.info("Delivered digest to recipient %d", index)
        LOG.info(
            "Completed: %d unique selections; %d emails sent",
            stats["selected_unique"],
            stats["emails_sent"],
        )
        return {**stats, "output_dir": str(run_dir)}
