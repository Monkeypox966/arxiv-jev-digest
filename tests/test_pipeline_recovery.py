"""Integration contracts for budgets, durable outbox, catch-up and metadata retries."""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from conftest import atom
from test_pipeline import NOW, Mailer, counts, transport

from arxiv_digest.arxiv import parse_atom
from arxiv_digest.cli import main
from arxiv_digest.clients import decision_key, fingerprint, profile_key
from arxiv_digest.config import load_config
from arxiv_digest.http import ServiceError
from arxiv_digest.models import Affiliation, PaperDetails
from arxiv_digest.pipeline import run
from arxiv_digest.store import Store, recipient_key
from arxiv_digest.usage import BudgetExceeded

RECIPIENT = recipient_key("reader@example.com")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def checkpoint_dates(config):
    with Store(config.state_dir) as store:
        return [
            row[0]
            for row in store.db.execute(
                "SELECT announcement FROM checkpoints WHERE recipient=?", (RECIPIENT,)
            )
        ]


def test_budget_failure_writes_health_and_usage_then_resumes_completed_decision(config, summary):
    config.budget.max_jev_calls = 1
    config.jev.concurrency = 1
    calls, messages = counts(), []
    with httpx.Client(transport=transport(config, summary, calls)) as client:
        with pytest.raises(BudgetExceeded):
            run(config, client, mailer_factory=lambda _: Mailer(messages), now=NOW)

    assert calls == {"arxiv": 1, "jev": 1, "summary": 0}
    assert messages == []
    health_path = next(config.output_dir.glob("*/health.json"))
    health, usage = read_json(health_path), read_json(health_path.with_name("usage.json"))
    assert health["status"] == "failed"
    assert health["error_type"] == "BudgetExceeded"
    assert usage["budget_exhausted"] is True
    assert usage["totals"]["jev_calls"] == 1
    assert usage["denied_attempts"] == 1
    first_paper = parse_atom(atom(["2610.00001v1"]))[0][0]
    with Store(config.state_dir) as store:
        assert store.cached("decision", decision_key(config, first_paper)) is not None
        assert store.pending() == []

    # Raising the run budget does not change the research profile or discard the
    # paid first result. Only the two remaining papers should need new decisions.
    config.budget.max_jev_calls = 3
    with httpx.Client(transport=transport(config, summary, calls)) as client:
        result = run(config, client, mailer_factory=lambda _: Mailer(messages), now=NOW)
    assert result["jev_calls"] == 2
    assert calls == {"arxiv": 2, "jev": 3, "summary": 2}
    assert len(messages) == 1
    assert read_json(Path(result["output_dir"]) / "health.json")["status"] == "success"


def test_outbox_persistence_failure_blocks_smtp_and_leaves_pending(config, summary):
    config.selection.top_k = 1
    calls, messages, events = counts(), [], []

    class ObservedMailer(Mailer):
        def __enter__(self):
            events.append("authenticated")
            return self

        def send(self, message):
            events.append("send")
            super().send(message)

    def persist():
        with Store(config.state_dir) as second_connection:
            assert len(second_connection.pending(RECIPIENT)) == 1
        events.append("persist_failed")
        raise ServiceError("Simulated remote outbox persistence failure")

    with httpx.Client(transport=transport(config, summary, calls)) as client:
        with pytest.raises(ServiceError, match="outbox persistence"):
            run(
                config,
                client,
                mailer_factory=lambda _: ObservedMailer(messages),
                outbox_hook=persist,
                now=NOW,
            )
        assert events == ["authenticated", "persist_failed"]
        assert messages == []
        with Store(config.state_dir) as store:
            assert len(store.pending(RECIPIENT)) == 1
        before = dict(calls)
        with pytest.raises(ServiceError, match="Unresolved email attempt"):
            run(config, client, mailer_factory=lambda _: ObservedMailer(messages), now=NOW)
    assert calls == before


def test_outbox_is_committed_and_persisted_after_login_before_smtp_send(config, summary):
    config.selection.top_k = 1
    messages, events, pending_id = [], [], []

    class ObservedMailer(Mailer):
        def __enter__(self):
            events.append("authenticated")
            return self

        def send(self, message):
            assert events == ["authenticated", "pending_persisted"]
            with Store(config.state_dir) as second_connection:
                assert second_connection.pending(RECIPIENT)[0]["id"] == pending_id[0]
            assert message["Message-ID"].startswith("<" + pending_id[0] + "@")
            events.append("send")
            super().send(message)

    def persist():
        assert events == ["authenticated"]
        with Store(config.state_dir) as second_connection:
            pending_id.append(second_connection.pending(RECIPIENT)[0]["id"])
        events.append("pending_persisted")

    with httpx.Client(transport=transport(config, summary, counts())) as client:
        result = run(
            config,
            client,
            mailer_factory=lambda _: ObservedMailer(messages),
            outbox_hook=persist,
            now=NOW,
        )
    assert events == ["authenticated", "pending_persisted", "send"]
    assert result["emails_sent"] == len(messages) == 1
    with Store(config.state_dir) as store:
        assert store.pending(RECIPIENT) == []


@pytest.mark.parametrize(
    "outcome", ["preview", "summary_failure", "success", "no_match", "uncertain"]
)
def test_announcement_checkpoint_advances_only_after_complete_recipient_processing(
    config, summary, monkeypatch, outcome
):
    config.arxiv.source, config.arxiv.catch_up = "latest", True
    config.arxiv.categories = ["cs.RO"]
    paper = replace(parse_atom(atom(["2610.00001v1"]))[0][0], announced="2026-10-23")
    source_calls = []

    def fetch(_settings, _client, **kwargs):
        source_calls.append(kwargs.get("since"))
        return [paper]

    monkeypatch.setattr("arxiv_digest.pipeline.fetch_papers", fetch)
    messages = []
    if outcome == "no_match":
        config.selection.min_score = 0.99
    with httpx.Client(
        transport=transport(config, summary, counts(), summary_fails=outcome == "summary_failure")
    ) as client:
        if outcome in {"summary_failure", "uncertain"}:
            with pytest.raises(ServiceError):
                run(
                    config,
                    client,
                    mailer_factory=lambda _: Mailer(
                        messages, "reader@example.com" if outcome == "uncertain" else None
                    ),
                    now=NOW,
                )
        else:
            result = run(
                config,
                client,
                dry_run=outcome == "preview",
                mailer_factory=lambda _: Mailer(messages),
                now=NOW,
            )
            if outcome == "no_match":
                assert result["emails_sent"] == 0
    assert source_calls == [None]
    assert checkpoint_dates(config) == (
        ["2026-10-23"] if outcome in {"success", "no_match"} else []
    )
    if outcome in {"preview", "summary_failure", "no_match"}:
        assert messages == []


def test_missing_affiliations_retry_after_latest_batch_changes_and_sent_stays_deduplicated(
    config, summary, monkeypatch
):
    config.enrichment.enabled = config.ranking.enabled = True
    config.ranking.unknown_policy = "exclude"
    config.enrichment.incomplete_cache_hours = 24
    older, newer = parse_atom(atom(["2610.00001v1", "2610.00003"]))[0]
    older = replace(older, announced="2026-10-23")
    newer = replace(newer, announced="2026-10-24")
    fetch_calls, enrichment_calls = [], []

    def fetch(_settings, _client, **kwargs):
        fetch_calls.append(kwargs["now"])
        return [older] if len(fetch_calls) == 1 else [newer]

    def enrich(_self, paper):
        enrichment_calls.append(paper.id)
        assert paper.id == older.id
        if len(enrichment_calls) == 1:
            return PaperDetails(
                project_url="https://project.example/paper",
                project_source=paper.pdf_url,
                sources=[paper.pdf_url],
                affiliation_status="unavailable",
                failed_sources=[paper.pdf_url],
            )
        return PaperDetails(
            project_url="https://project.example/paper",
            project_source=paper.pdf_url,
            sources=[paper.pdf_url],
            affiliation_status="found",
            affiliations=[
                Affiliation(institution="MIT", authors=paper.authors, source=paper.pdf_url)
            ],
        )

    monkeypatch.setattr("arxiv_digest.pipeline.fetch_papers", fetch)
    monkeypatch.setattr("arxiv_digest.enrichment.Enricher.enrich", enrich)
    calls, messages = counts(), []
    with httpx.Client(transport=transport(config, summary, calls)) as client:
        first = run(config, client, mailer_factory=lambda _: Mailer(messages), now=NOW)
        assert first["selected_unique"] == 0
        assert messages == []
        with Store(config.state_dir) as store:
            assert (
                store.due_metadata(
                    RECIPIENT, profile_key(config), now=NOW + timedelta(hours=23), limit=20
                )
                == []
            )
            due = store.due_metadata(
                RECIPIENT, profile_key(config), now=NOW + timedelta(hours=24), limit=20
            )
            assert [p["id"] for p in due] == [older.id]

        early = run(
            config, client, mailer_factory=lambda _: Mailer(messages), now=NOW + timedelta(hours=23)
        )
        assert early["metadata_retries"] == early["selected_unique"] == 0
        assert enrichment_calls == [older.id]
        recovered = run(
            config, client, mailer_factory=lambda _: Mailer(messages), now=NOW + timedelta(hours=25)
        )
        assert recovered["metadata_retries"] == recovered["selected_unique"] == 1
        assert recovered["emails_sent"] == 1
        report = read_json(Path(recovered["output_dir"]) / "digest.json")
        assert [p["paper"]["id"] for p in report["papers"]] == [older.id]
        assert older.url in messages[0].get_body(preferencelist=("plain",)).get_content()
        final = run(
            config, client, mailer_factory=lambda _: Mailer(messages), now=NOW + timedelta(hours=50)
        )
    assert final["metadata_retries"] == final["emails_sent"] == 0
    assert len(messages) == 1
    assert enrichment_calls == [older.id, older.id]
    assert calls == {"arxiv": 0, "jev": 2, "summary": 1}


def test_smtp_login_failures_do_not_exhaust_or_postpone_cached_metadata_retries(
    config, summary, monkeypatch
):
    config.enrichment.enabled = config.ranking.enabled = True
    config.ranking.unknown_policy = "exclude"
    config.enrichment.incomplete_cache_hours = 24
    papers = parse_atom(atom(["2610.00001v1", "2610.00002v2"]))[0]
    metadata_calls = []
    monkeypatch.setattr("arxiv_digest.pipeline.fetch_papers", lambda *_args, **_kwargs: papers)

    def enrich(_self, paper):
        metadata_calls.append(paper.id)
        return PaperDetails(
            project_url="https://project.example/paper",
            project_source=paper.pdf_url,
            sources=[paper.pdf_url],
            affiliations=(
                []
                if paper.id == papers[0].id
                else [Affiliation(institution="MIT", authors=paper.authors, source=paper.pdf_url)]
            ),
        )

    monkeypatch.setattr("arxiv_digest.enrichment.Enricher.enrich", enrich)

    class LoginFailure:
        def __enter__(self):
            raise ServiceError("Simulated SMTP login failure")

        def __exit__(self, *_):
            pass

    with httpx.Client(transport=transport(config, summary, counts())) as client:
        for hours in (0, 1, 2):
            with pytest.raises(ServiceError, match="SMTP login"):
                run(
                    config,
                    client,
                    mailer_factory=lambda _: LoginFailure(),
                    now=NOW + timedelta(hours=hours),
                )
    assert metadata_calls == [p.id for p in papers]
    with Store(config.state_dir) as store:
        row = store.db.execute(
            "SELECT attempts, next_retry FROM metadata_retry "
            "WHERE recipient=? AND profile=? AND paper=?",
            (RECIPIENT, profile_key(config), papers[0].id),
        ).fetchone()
    assert row == (1, (NOW + timedelta(hours=24)).isoformat())


def test_quieter_category_catches_up_even_when_another_category_has_a_newer_latest_date(
    config, summary, monkeypatch
):
    config.arxiv.source, config.arxiv.catch_up = "latest", True
    config.arxiv.categories = ["cs.RO", "cs.LG"]
    first, relevant_quiet, irrelevant = parse_atom(
        atom(["2610.00001v1", "2610.00002v2", "2610.00003"])
    )[0]
    rounds = [
        {
            "cs.RO": [replace(first, announced="2026-10-23")],
            "cs.LG": [replace(irrelevant, announced="2026-10-22")],
        },
        {
            "cs.RO": [replace(irrelevant, announced="2026-10-24")],
            "cs.LG": [replace(relevant_quiet, announced="2026-10-23")],
        },
    ]
    phase, source_calls = [0], []

    def fetch(settings, _client, **kwargs):
        assert len(settings.categories) == 1
        category = settings.categories[0]
        source_calls.append((category, kwargs.get("since")))
        return rounds[phase[0]][category]

    monkeypatch.setattr("arxiv_digest.pipeline.fetch_papers", fetch)
    calls, messages = counts(), []
    with httpx.Client(transport=transport(config, summary, calls)) as client:
        run(config, client, mailer_factory=lambda _: Mailer(messages), now=NOW)
        assert sorted(checkpoint_dates(config)) == ["2026-10-22", "2026-10-23"]
        phase[0] = 1
        result = run(
            config, client, mailer_factory=lambda _: Mailer(messages), now=NOW + timedelta(days=1)
        )
    assert source_calls == [
        ("cs.RO", None),
        ("cs.LG", None),
        ("cs.RO", "2026-10-23"),
        ("cs.LG", "2026-10-22"),
    ]
    assert result["selected_unique"] == result["emails_sent"] == 1
    assert len(messages) == 2
    assert relevant_quiet.url in messages[1].get_body(preferencelist=("plain",)).get_content()
    assert sorted(checkpoint_dates(config)) == ["2026-10-23", "2026-10-24"]


def recovery_config_file(directory):
    path = directory / "config.toml"
    path.write_text(
        'state_dir = ".state"\noutput_dir = "output"\n'
        '[arxiv]\ncategories = ["cs.RO", "cs.LG"]\nsource = "latest"\n'
        "lookback_days = 3\ncatch_up = true\n"
        '[[interests]]\nid = "robotics"\nlabel = "Robotics"\n'
        'text = "The paper studies humanoid robot control."\n'
        '[email]\nrecipients = ["config-reader@example.com"]\n',
        encoding="utf-8",
    )
    return path


def test_cli_recovery_window_is_in_memory_only_and_preview_has_no_outbox_hook(
    config, tmp_path, monkeypatch
):
    path = recovery_config_file(tmp_path)
    original = path.read_bytes()
    calls = []

    def observed_run(settings, _client, *, dry_run, outbox_hook):
        calls.append(settings)
        assert settings.arxiv.source == "api"
        assert settings.arxiv.lookback_days == 7
        assert settings.arxiv.catch_up is False
        assert dry_run is True
        assert outbox_hook is None
        return {"emails_sent": 0}

    monkeypatch.setattr("arxiv_digest.cli.run", observed_run)
    assert main(["--config", str(path), "run", "--dry-run", "--recovery-days", "7"]) == 0
    assert len(calls) == 1
    assert path.read_bytes() == original
    source = load_config(path)
    assert source.arxiv.source == "latest"
    assert source.arxiv.lookback_days == 3
    assert source.arxiv.catch_up is True
    assert not source.state_dir.exists()


def test_cli_checkpoint_reset_is_scoped_and_preserves_delivery_history(config, tmp_path):
    path = recovery_config_file(tmp_path)
    settings = load_config(path)

    def source(category, cross_lists=True):
        return fingerprint({"categories": [category], "cross_lists": cross_lists, "mode": "latest"})

    robotics, learning = source("cs.RO"), source("cs.LG")
    unrelated, different_filter = source("cs.AI"), source("cs.RO", cross_lists=False)
    other_recipient = recipient_key("other@example.com")
    configured_recipient = recipient_key("config-reader@example.com")
    profile = profile_key(settings)
    with Store(settings.state_dir) as store:
        delivery = store.prepare(
            RECIPIENT,
            profile,
            ["2610.00001", "2610.00002"],
            ["2610.00001"],
            checkpoint_updates=[(robotics, "2026-10-23"), (learning, "2026-10-22")],
        )
        store.resolve(delivery, "sent")
        store.mark_reviewed(RECIPIENT, profile, [], source=unrelated, announcement="2026-10-21")
        store.mark_reviewed(
            RECIPIENT, profile, [], source=different_filter, announcement="2026-10-20"
        )
        store.mark_reviewed(
            other_recipient, profile, [], source=robotics, announcement="2026-10-24"
        )
        store.mark_reviewed(
            configured_recipient, profile, [], source=robotics, announcement="2026-10-25"
        )

    # EMAIL_TO from the test environment overrides recipients in config.toml.
    assert main(["--config", str(path), "reset-checkpoints"]) == 0
    with Store(settings.state_dir) as store:
        assert store.checkpoint(RECIPIENT, robotics) is None
        assert store.checkpoint(RECIPIENT, learning) is None
        assert store.checkpoint(RECIPIENT, unrelated) == "2026-10-21"
        assert store.checkpoint(RECIPIENT, different_filter) == "2026-10-20"
        assert store.checkpoint(other_recipient, robotics) == "2026-10-24"
        assert store.checkpoint(configured_recipient, robotics) == "2026-10-25"
        assert not store.eligible(RECIPIENT, "another-profile", "2610.00001")
        assert not store.eligible(RECIPIENT, profile, "2610.00002")
        assert store.db.execute("SELECT count(*) FROM sent").fetchone()[0] == 1
        assert store.db.execute("SELECT count(*) FROM reviewed").fetchone()[0] == 2
        assert store.db.execute("SELECT status FROM outbox WHERE id=?", (delivery,)).fetchone() == (
            "sent",
        )


def test_deferred_papers_do_not_bypass_a_changed_category_filter(config, paper, monkeypatch):
    from dataclasses import replace

    config.arxiv.categories = ["cs.CL"]
    config.enrichment.enabled = True
    robot_paper = replace(paper, categories=["cs.RO"])
    with Store(config.state_dir) as store:
        store.mark_reviewed(recipient_key("reader@example.com"), profile_key(config), [paper.id])
        store.queue_metadata_retry(
            recipient_key("reader@example.com"),
            profile_key(config),
            robot_paper.to_dict(),
            delay_hours=1,
            max_attempts=3,
            now=NOW - timedelta(days=1),
        )
    monkeypatch.setattr("arxiv_digest.pipeline.fetch_papers", lambda *_args, **_kwargs: [])

    def unexpected(request):
        raise AssertionError(
            f"Old out-of-category paper must not trigger a model call: {request.url}"
        )

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        result = run(config, client, dry_run=True, now=NOW)
    assert result["new_candidates"] == result["metadata_retries"] == 0
