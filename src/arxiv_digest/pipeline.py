from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .arxiv import fetch_papers
from .clients import (
    DeepSeekClient,
    JevClient,
    decision_key,
    profile_key,
    summary_key,
)
from .config import Config, recipients, validate_credentials
from .http import ServiceError
from .mailer import SMTPMailer, build_message
from .models import Decision, Pick, Summary
from .report import write_report
from .store import Store, recipient_key

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
) -> dict:
    validate_credentials(config, send=not dry_run)
    now = (now or datetime.now(ZoneInfo(config.timezone))).astimezone(ZoneInfo(config.timezone))
    run_dir = config.output_dir / (now.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    addresses = recipients(config)
    # A dry run without configured recipients uses a synthetic preview identity.
    targets = addresses or ["preview@example.invalid"]
    profile = profile_key(config)
    with Store(config.state_dir) as store:
        store.prune(config.retention.cache_days)
        if not dry_run:
            for address in targets:
                pending = store.pending(recipient_key(address))
                if pending:
                    raise ServiceError(
                        f"Unresolved email attempt {pending[0]['id']}; "
                        "check inbox, then use `pending` and `resolve`"
                    )
        papers = fetch_papers(config.arxiv, client, now=now)
        LOG.info("Fetched %d unique candidate papers", len(papers))
        pools = {
            address: [p for p in papers if store.eligible(recipient_key(address), profile, p.id)]
            for address in targets
        }
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
        jev = JevClient(config, client)
        for completed, (paper, decision) in enumerate(
            bounded_map(jev.judge, uncached, config.jev.concurrency), 1
        ):
            decisions[paper.id] = decision
            store.cache("decision", decision_key(config, paper), decision.model_dump())
            if completed % 100 == 0 or completed == len(uncached):
                LOG.info("Jev screening progress: %d/%d", completed, len(uncached))
        selected = {}
        for address, pool in pools.items():
            ranked = sorted(
                (
                    p
                    for p in pool
                    if not decisions[p.id].excluded
                    and decisions[p.id].score >= config.selection.min_score
                ),
                key=lambda p: (-decisions[p.id].score, p.id),
            )
            selected[address] = ranked[: config.selection.top_k]
        summary_papers = {p.id: p for group in selected.values() for p in group}
        summaries = {}
        unsummarized = []
        for paper in summary_papers.values():
            cached = store.cached("summary", summary_key(config, paper))
            if cached:
                summaries[paper.id] = Summary.model_validate(cached)
            else:
                unsummarized.append(paper)
        LOG.info("Summarizing %d selected papers (%d cached)", len(unsummarized), len(summaries))
        deepseek = DeepSeekClient(config, client)
        for completed, (paper, summary) in enumerate(
            bounded_map(deepseek.summarize, unsummarized, config.deepseek.concurrency), 1
        ):
            summaries[paper.id] = summary
            store.cache("summary", summary_key(config, paper), summary.model_dump())
            if completed % 5 == 0 or completed == len(unsummarized):
                LOG.info("DeepSeek summary progress: %d/%d", completed, len(unsummarized))
        stats = {
            "fetched": len(papers),
            "new_candidates": len(wanted),
            "jev_calls": len(uncached),
            "summary_calls": len(unsummarized),
            "selected_unique": len(summary_papers),
            "emails_sent": 0,
            "dry_run": dry_run,
        }
        for index, address in enumerate(targets, 1):
            picks = [Pick(p, decisions[p.id], summaries[p.id]) for p in selected[address]]
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
            candidates = [p.id for p in pools[address]]
            if not picks and not config.email.send_empty:
                store.mark_reviewed(key, profile, candidates)
                LOG.info("No new matches for recipient %d; email skipped", index)
                continue
            # Authenticate before preparing an outbox entry: a failed login cannot send mail.
            with mailer_factory(config) as mailer:
                delivery = store.prepare(key, profile, candidates, [p.paper.id for p in picks])
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
