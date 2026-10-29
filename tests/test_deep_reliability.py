"""Regression tests for missed institutions, deferred decisions and delivery recovery."""

import json
import smtplib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_enrichment_fallbacks import PROJECT, abs_page, source_details
from test_enrichment_fallbacks import prepare as prepare
from test_pipeline import Mailer

from arxiv_digest.clients import profile_key
from arxiv_digest.enrichment import Enricher
from arxiv_digest.http import ServiceError
from arxiv_digest.institution_assessment import InstitutionAssessor
from arxiv_digest.mailer import SMTPMailer, send_prepared
from arxiv_digest.models import Affiliation, Decision, PaperDetails
from arxiv_digest.pipeline import run
from arxiv_digest.ranking import passes_institution_filter, rank_paper
from arxiv_digest.store import Store, recipient_key

NOW = datetime(2026, 10, 26, 4, tzinfo=UTC)
KEY = recipient_key("reader@example.com")
QUOTE = "We published reproducible humanoid control research with substantial experimental results."


def test_partial_html_cannot_hide_pdf_or_project_collaborators(config, paper, prepare):
    enricher, events = prepare(
        paper,
        html={
            paper.url: abs_page(paper, project=PROJECT),
            paper.metadata_html_url: "<article>Harvard University</article>",
            PROJECT: f"<article>{paper.title} Stanford University</article>",
        },
        pdf=("MIT", []),
        extracted={
            paper.url: source_details(paper, paper.url, project=PROJECT),
            paper.metadata_html_url: source_details(
                paper, paper.metadata_html_url, affiliations=[("Harvard University", [])]
            ),
            paper.pdf_url: source_details(paper, paper.pdf_url, affiliations=[("MIT", [])]),
            PROJECT: source_details(paper, PROJECT, affiliations=[("Stanford University", [])]),
        },
    )
    details = enricher.enrich(paper)
    assert {a.institution for a in details.affiliations} == {
        "Harvard University",
        "MIT",
        "Stanford University",
    }
    assert ("pdf", paper.pdf_url) in events and ("fetch", PROJECT) in events
    assert rank_paper(paper, 0.9, details, config.ranking).institution_score == 1


def test_failed_pdf_with_existing_institutions_is_retried_after_short_cache(config, paper):
    enricher = Enricher(config, None, None)
    details = source_details(
        paper, paper.url, project=PROJECT, affiliations=[("Harvard University", [])]
    )
    details.failed_sources = [paper.pdf_url]
    with Store(config.state_dir) as store:
        store.cache("details", enricher.key(paper), details.model_dump(), now=NOW)
        assert enricher.cached_details(store, paper, now=NOW).affiliation_status == "partial"
        assert enricher.cached_details(store, paper, now=NOW + timedelta(hours=25)) is None


def setup_pipeline(config, paper, summary, monkeypatch):
    config.ranking.enabled = config.ranking.assess_unlisted = config.enrichment.enabled = True
    config.ranking.institution_weight = 0.35
    config.ranking.min_institution_score = 0.8
    config.ranking.assessment_limit_per_run = 1
    config.selection.top_k = 1
    config.email.send_empty = True
    config.arxiv.categories = paper.categories
    monkeypatch.setattr("arxiv_digest.clients.DeepSeekClient.summarize", lambda *_a: summary)
    monkeypatch.setattr(
        "arxiv_digest.clients.JevClient.judge",
        lambda *_a: Decision(score=0.9, interests={}, exclusions={}, excluded=False, model="test"),
    )
    monkeypatch.setattr(
        Enricher,
        "enrich",
        lambda _self, p: source_details(
            p, p.pdf_url, project=PROJECT, affiliations=[(p.title, [])]
        ),
    )
    monkeypatch.setattr(
        InstitutionAssessor,
        "evidence",
        lambda *_a: {PROJECT: QUOTE, PROJECT + "research": QUOTE + " A separate benchmark."},
    )

    def grade(_self, _system, context, parser, **_kwargs):
        score = 0.8 if context["institution"] == "Unlisted One" else 1
        return parser(
            json.dumps(
                {
                    "institution": context["institution"],
                    "score": score,
                    "confidence": 0.99,
                    "evidence": [{"source": u, "quote": t} for u, t in context["pages"].items()],
                }
            )
        )

    monkeypatch.setattr("arxiv_digest.clients.DeepSeekClient.complete_json", grade)


def test_capped_assessment_survives_review_history_and_leaving_latest_batch(
    config, paper, summary, monkeypatch
):
    setup_pipeline(config, paper, summary, monkeypatch)
    first = replace(paper, title="Unlisted One")
    second = replace(paper, id="2610.00002", title="Unlisted Two")
    phase, lookups, messages = [0], [], []
    monkeypatch.setattr(
        "arxiv_digest.pipeline.fetch_papers",
        lambda *_a, **_k: [first, second] if phase[0] == 0 else [],
    )

    def resolve(_self, name):
        lookups.append(name)
        return "https://ror.org/" + name.replace(" ", ""), name, PROJECT

    monkeypatch.setattr(InstitutionAssessor, "resolve", resolve)
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail(str(r.url)))) as client:
        initial = run(config, client, mailer_factory=lambda _: Mailer(messages), now=NOW)
        assert initial["institution_assessment_lookups"] == initial["selected_unique"] == 1
        with Store(config.state_dir) as store:
            assert store.due_assessments(KEY, profile_key(config), now=NOW, limit=20) == []
            assert store.db.execute("SELECT count(*) FROM assessment_retry").fetchone()[0] == 1
        phase[0] = 1
        recovered = run(
            config, client, mailer_factory=lambda _: Mailer(messages), now=NOW + timedelta(hours=25)
        )
        final = run(
            config, client, mailer_factory=lambda _: Mailer(messages), now=NOW + timedelta(days=8)
        )
    assert recovered["institution_assessment_retries"] == recovered["selected_unique"] == 1
    assert second.url in messages[1].get_body(preferencelist=("plain",)).get_content()
    assert final["institution_assessment_retries"] == 0
    assert lookups == ["Unlisted One", "Unlisted Two"]
    with Store(config.state_dir) as store:
        assert store.db.execute("SELECT count(*) FROM assessment_retry").fetchone()[0] == 0


def test_neutral_assessment_retries_after_evidence_failure_without_resending(
    config, paper, summary, monkeypatch
):
    setup_pipeline(config, paper, summary, monkeypatch)
    config.ranking.unlisted_policy = "exclude"
    novel = replace(paper, title="Unlisted Two")
    phase, messages = [0], []
    monkeypatch.setattr(
        "arxiv_digest.pipeline.fetch_papers", lambda *_a, **_k: [novel] if phase[0] == 0 else []
    )

    def resolve(_self, name):
        if phase[0] == 0:
            raise ValueError("Registry temporarily unavailable")
        return "https://ror.org/novel", name, PROJECT

    monkeypatch.setattr(InstitutionAssessor, "resolve", resolve)
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail(str(r.url)))) as client:
        initial = run(config, client, mailer_factory=lambda _: Mailer(messages), now=NOW)
        phase[0] = 1
        recovered = run(
            config, client, mailer_factory=lambda _: Mailer(messages), now=NOW + timedelta(days=8)
        )
    assert initial["selected_unique"] == 0
    assert recovered["institution_assessment_retries"] == recovered["selected_unique"] == 1


def test_assessment_retry_is_recipient_scoped_and_not_postponed_by_failed_delivery(config, paper):
    with Store(config.state_dir) as store:
        due = NOW + timedelta(days=1)
        store.queue_assessment_retry(KEY, "profile", paper.to_dict(), retry_at=due, now=NOW)
        store.queue_assessment_retry(
            KEY, "profile", paper.to_dict(), retry_at=due + timedelta(days=1), now=NOW
        )
        store.mark_reviewed(KEY, "profile", [paper.id])
        assert not store.eligible(KEY, "profile", paper.id, now=NOW)
        assert store.eligible(KEY, "profile", paper.id, now=due)
        assert store.due_assessments("other", "profile", now=due, limit=20) == []
        assert store.due_assessments(KEY, "other-profile", now=due, limit=20) == []


@pytest.mark.parametrize("retry_kind", ["assessment", "metadata"])
def test_recovered_paper_survives_smtp_login_failure_after_leaving_feed(
    config, paper, summary, monkeypatch, retry_kind
):
    setup_pipeline(config, paper, summary, monkeypatch)
    config.ranking.unknown_policy = config.ranking.unlisted_policy = "exclude"
    novel = replace(paper, title="Unlisted Two")
    phase, messages = [0], []
    monkeypatch.setattr(
        "arxiv_digest.pipeline.fetch_papers", lambda *_a, **_k: [novel] if phase[0] == 0 else []
    )

    def resolve(_self, name):
        if phase[0] == 0:
            raise ValueError("Evidence unavailable")
        return "https://ror.org/novel", name, PROJECT

    monkeypatch.setattr(InstitutionAssessor, "resolve", resolve)
    if retry_kind == "metadata":
        monkeypatch.setattr(
            Enricher,
            "enrich",
            lambda _s, p: source_details(
                p, p.pdf_url, project=PROJECT, affiliations=[] if phase[0] == 0 else [("MIT", [])]
            ),
        )

    class LoginFailure:
        def __enter__(self):
            raise ServiceError("SMTP login failed")

        def __exit__(self, *_args):
            pass

    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail(str(r.url)))) as client:
        run(config, client, mailer_factory=lambda _: Mailer(messages), now=NOW)
        phase[0] = 1
        due = NOW + timedelta(days=8)
        with pytest.raises(ServiceError, match="login failed"):
            run(config, client, mailer_factory=lambda _: LoginFailure(), now=due)
        with Store(config.state_dir) as store:
            assert store.eligible(KEY, profile_key(config), novel.id, now=due)
            assert not store.pending()
        recovered = run(
            config, client, mailer_factory=lambda _: Mailer(messages), now=due + timedelta(hours=1)
        )
    assert recovered["selected_unique"] == recovered["emails_sent"] == 1
    assert len(messages) == 1  # The initial no-match run no longer sends an empty digest.
    assert novel.url in messages[-1].get_body(preferencelist=("plain",)).get_content()


@pytest.mark.parametrize("fail_at", [1, 2])
def test_pre_smtp_persistence_failures_are_safe_to_retry(config, fail_at):
    calls, remote, sends = [], [], []
    with Store(config.state_dir) as store:
        delivery = store.prepare(KEY, "profile", [], [], staged=True)

        def persist():
            rows = store.db.execute("SELECT status FROM outbox").fetchall()
            remote[:] = rows
            calls.append(rows)
            if len(calls) == fail_at:
                raise ServiceError("Simulated persistence failure")

        class Mail:
            def send(self, _message):
                sends.append(True)

        with pytest.raises(ServiceError, match="persistence"):
            send_prepared(store, delivery, Mail(), None, outbox_hook=persist)
        assert not sends and not store.pending() and not remote
        assert store.eligible(KEY, "profile", "paper")


def test_crash_recovery_discards_only_provably_unsent_preparation(config):
    with Store(config.state_dir) as store:
        store.prepare(KEY, "profile", [], [], staged=True)
        uncertain = store.prepare("other", "profile", [], [])
    with Store(config.state_dir) as restored:
        restored.discard_prepared(KEY)
        restored.discard_prepared("other")
        assert [r["id"] for r in restored.pending()] == [uncertain]
        restored.prepare(KEY, "profile", [], [], staged=True)


@pytest.mark.parametrize(
    "error",
    [
        smtplib.SMTPDataError(550, b"rejected"),
        smtplib.SMTPRecipientsRefused({"reader@example.com": (550, b"rejected")}),
        smtplib.SMTPSenderRefused(550, b"rejected", "sender@example.com"),
        smtplib.SMTPServerDisconnected("connection lost"),
    ],
)
def test_only_explicit_smtp_rejections_clear_uncertain_attempt(config, error):
    class SMTP:
        def send_message(self, _message):
            raise error

    mailer = SMTPMailer(config)
    mailer.smtp = SMTP()
    with Store(config.state_dir) as store:
        delivery = store.prepare(KEY, "profile", [], [], staged=True)
        with pytest.raises(ServiceError):
            send_prepared(store, delivery, mailer, None)
        assert bool(store.pending()) == isinstance(error, smtplib.SMTPServerDisconnected)


def test_known_b_tier_is_filtered_without_treating_missing_evidence_as_low_quality(config, paper):
    config.ranking.enabled = True
    config.ranking.min_institution_score = 0.8
    details = PaperDetails(
        affiliations=[Affiliation(institution="Harvard University", source=paper.pdf_url)]
    )
    assert not passes_institution_filter(
        rank_paper(paper, 0.99, details, config.ranking), config.ranking
    )
    assert passes_institution_filter(rank_paper(paper, 0.99, None, config.ranking), config.ranking)
