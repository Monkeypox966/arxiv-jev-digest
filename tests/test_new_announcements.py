from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from conftest import atom
from test_pipeline import Mailer

from arxiv_digest.arxiv import fetch_papers, parse_new_page
from arxiv_digest.clients import profile_key
from arxiv_digest.config import ArxivSettings
from arxiv_digest.http import ServiceError
from arxiv_digest.models import Decision, Pick
from arxiv_digest.pipeline import run
from arxiv_digest.report import render
from arxiv_digest.store import Store, recipient_key

NOW = datetime(2026, 10, 26, 5, tzinfo=UTC)


def new_page(groups, total=None, day="2026-10-26"):
    names = {
        "new": "New submissions",
        "cross": "Cross submissions",
        "replace": "Replacement submissions",
    }
    total = sum(expected for _, _, expected in groups) if total is None else total
    text = f"<h3>Showing new listings for {date.fromisoformat(day):%A, %d %B %Y}</h3>"
    text += f"<p>Total of {total} entries</p>"
    for kind, ids, expected in groups:
        text += (
            f'<dl id="articles"><h3>{names[kind]} (showing {len(ids)} of {expected} entries)</h3>'
        )
        text += "".join(
            f'<dt><a title="Abstract" href="/abs/{id}">arXiv:{id}</a></dt>' for id in ids
        )
        text += "</dl>"
    return text


def test_new_page_keeps_all_three_announcement_types():
    groups = [
        ("new", ["2610.00001"], 1),
        ("cross", ["2501.00001"], 1),
        ("replace", ["2401.00001"], 1),
    ]
    day, result, total = parse_new_page(new_page(groups))
    assert day == "2026-10-26" and result == groups and total == 3


def test_new_page_empty_is_valid_only_with_dated_zero_total():
    assert parse_new_page(new_page([])) == ("2026-10-26", [], 0)
    with pytest.raises(ServiceError, match="date"):
        parse_new_page("<h3>No changes</h3><p>Total of 0 entries</p>")


@pytest.mark.parametrize("problem", ["truncated", "duplicate", "unknown_section"])
def test_new_page_rejects_incomplete_or_unrecognized_sections(problem):
    page = new_page([("new", ["2610.00001"], 1)])
    if problem == "truncated":
        page = page.replace("showing 1", "showing 2")
    elif problem == "duplicate":
        page = new_page([("new", ["2610.00001", "2610.00001"], 2)])
    else:
        page = page.replace("New submissions", "Unexpected submissions")
    with pytest.raises(ServiceError):
        parse_new_page(page)


@pytest.mark.parametrize("include_cross", [True, False])
def test_new_source_uses_only_new_then_metadata_and_preserves_types(config, include_cross):
    config.arxiv.source = "new"
    config.arxiv.include_cross_lists = include_cross
    config.arxiv.categories = ["cs.RO"]
    groups = [
        ("new", ["2610.00001"], 1),
        ("cross", ["2501.00001"], 1),
        ("replace", ["2401.00001"], 1),
    ]
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "arxiv.org":
            assert request.url.path == "/list/cs.RO/new"
            return httpx.Response(200, text=new_page(groups))
        ids = request.url.params["id_list"].split(",")
        return httpx.Response(
            200,
            text=atom([id + ("v2" if id.startswith("2401") else "v1") for id in ids]).replace(
                "cs.AI", "cs.RO"
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        papers = fetch_papers(config.arxiv, client)
    assert {p.announcement_type for p in papers} == (
        {"new", "cross", "replace"} if include_cross else {"new", "replace"}
    )
    assert all(p.announced == "2026-10-26" for p in papers)
    assert (
        next(p for p in papers if p.announcement_type == "replace").tracking_id == "2401.00001@v2"
    )
    assert len(calls) == 2


def test_new_source_handles_pagination_split_inside_replacements(config):
    config.arxiv.source = "new"
    config.arxiv.categories = ["cs.RO"]
    pages = {
        0: new_page([("new", ["2610.00001"], 1), ("replace", ["2401.00001"], 2)], total=3),
        2: new_page([("replace", ["2401.00002"], 2)], total=3),
    }
    offsets = []

    def handler(request):
        if request.url.host == "arxiv.org":
            offset = int(request.url.params["skip"])
            offsets.append(offset)
            return httpx.Response(200, text=pages[offset])
        return httpx.Response(
            200, text=atom([id + "v2" for id in request.url.params["id_list"].split(",")])
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        papers = fetch_papers(config.arxiv, client)
    assert len(papers) == 3 and offsets == [0, 2]


@pytest.mark.parametrize("problem", ["date", "total", "duplicate", "missing_version"])
def test_new_source_fails_visibly_when_batch_changes_or_revision_cannot_be_deduplicated(
    config, problem
):
    config.arxiv.source = "new"

    def handler(request):
        if request.url.host != "arxiv.org":
            return httpx.Response(200, text=atom(["2401.00001"]))
        if problem == "missing_version":
            return httpx.Response(200, text=new_page([("replace", ["2401.00001"], 1)]))
        if request.url.params["skip"] == "0":
            return httpx.Response(200, text=new_page([("new", ["2610.00001"], 2)], total=2))
        return httpx.Response(
            200,
            text=new_page(
                [("new", ["2610.00001" if problem == "duplicate" else "2610.00002"], 2)],
                total=3 if problem == "total" else 2,
                day="2026-10-27" if problem == "date" else "2026-10-26",
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ServiceError):
            fetch_papers(config.arxiv, client)


def test_new_source_does_not_claim_historical_catchup():
    with pytest.raises(ValueError, match="no history"):
        ArxivSettings(categories=["cs.RO"], source="new", catch_up=True)


def test_version_history_is_acknowledged_atomically_and_independent_of_profile(config, paper):
    key = recipient_key("reader@example.com")
    with Store(config.state_dir) as store:
        delivery = store.prepare(
            key, "old", [paper.id], [paper.id], selected_versions=[(paper.id, "v1")]
        )
        store.resolve(delivery, "sent")
        assert not store.eligible(key, "new", paper.id)
        assert not store.eligible(key, "new", paper.id + "@v1")
        assert store.eligible(key, "new", paper.id + "@v2")
        pending = store.prepare(
            key, "new", [paper.id + "@v2"], [paper.id], selected_versions=[(paper.id, "v2")]
        )
        store.resolve(pending, "retry")
        assert store.eligible(key, "new", paper.id + "@v2")


def test_revision_pipeline_sends_each_version_once_and_groups_email(
    config, paper, summary, monkeypatch
):
    phase, messages = [1], []
    monkeypatch.setattr(
        "arxiv_digest.pipeline.fetch_papers",
        lambda *_a, **_k: [
            replace(
                paper,
                version=f"v{phase[0]}",
                announcement_type="new" if phase[0] == 1 else "replace",
            )
        ],
    )
    monkeypatch.setattr(
        "arxiv_digest.clients.JevClient.judge",
        lambda *_a: Decision(score=0.9, interests={}, exclusions={}, excluded=False, model="test"),
    )
    monkeypatch.setattr("arxiv_digest.clients.DeepSeekClient.summarize", lambda *_a: summary)
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail(str(r.url)))) as client:
        first = run(config, client, now=NOW, mailer_factory=lambda _: Mailer(messages))
        phase[0] = 2
        revision = run(
            config, client, now=NOW + timedelta(days=1), mailer_factory=lambda _: Mailer(messages)
        )
        repeated = run(
            config, client, now=NOW + timedelta(days=2), mailer_factory=lambda _: Mailer(messages)
        )
        phase[0] = 3
        third = run(
            config, client, now=NOW + timedelta(days=3), mailer_factory=lambda _: Mailer(messages)
        )
    assert first["selected_unique"] == revision["selected_unique"] == third["selected_unique"] == 1
    assert repeated["selected_unique"] == 0 and len(messages) == 3
    body = messages[1].get_body(preferencelist=("plain",)).get_content()
    assert "新提交 · 0 篇" in body and "跨分类 · 0 篇" in body and "修订论文 · 1 篇" in body
    assert paper.id + "v2" in body


def test_revision_metadata_retry_survives_original_paper_sent_history(config, paper):
    revision = replace(paper, version="v2", announcement_type="replace")
    key, profile = recipient_key("reader@example.com"), profile_key(config)
    with Store(config.state_dir) as store:
        original = store.prepare(key, profile, [paper.id], [paper.id])
        store.resolve(original, "sent")
        store.queue_metadata_retry(
            key, profile, revision.to_dict(), delay_hours=1, max_attempts=3, now=NOW
        )
        assert (
            store.due_metadata(key, profile, now=NOW + timedelta(hours=2), limit=20)[0]["version"]
            == "v2"
        )
        sent = store.prepare(
            key, profile, [revision.tracking_id], [paper.id], selected_versions=[(paper.id, "v2")]
        )
        store.resolve(sent, "sent")
        assert not store.due_metadata(key, profile, now=NOW + timedelta(hours=2), limit=20)


def test_email_groups_all_three_sections_without_hiding_empty_sections(config, paper, summary):
    decision = Decision(score=0.9, interests={}, exclusions={}, excluded=False, model="test")
    picks = [
        Pick(
            replace(paper, id=f"2610.{i:05}", announcement_type=kind, version="v2"),
            decision,
            summary,
        )
        for i, kind in enumerate(["replace", "cross", "new"], 1)
    ]
    _, text, html = render(config, picks, NOW, {"fetched": 3})
    assert text.index("新提交 · 1 篇") < text.index("跨分类 · 1 篇") < text.index("修订论文 · 1 篇")
    assert html.index("新提交 · 1 篇") < html.index("跨分类 · 1 篇") < html.index("修订论文 · 1 篇")


@pytest.mark.parametrize("qualified,expected", [(3, 3), (17, 15)])
def test_total_top_k_caps_all_sections_without_filling_with_unqualified_papers(
    config, paper, summary, monkeypatch, qualified, expected
):
    config.selection.top_k = 15
    papers = [
        replace(
            paper,
            id=f"2610.{i + 1:05}",
            title=str(i),
            version="v2",
            announcement_type=["new", "cross", "replace"][i % 3],
        )
        for i in range(qualified + 1)
    ]
    monkeypatch.setattr("arxiv_digest.pipeline.fetch_papers", lambda *_a, **_k: papers)
    monkeypatch.setattr(
        "arxiv_digest.clients.JevClient.judge",
        lambda _self, p: Decision(
            score=0.9 if int(p.title) < qualified else 0.2,
            interests={},
            exclusions={},
            excluded=False,
            model="test",
        ),
    )
    monkeypatch.setattr("arxiv_digest.clients.DeepSeekClient.summarize", lambda *_a: summary)
    messages = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail(str(r.url)))) as client:
        result = run(config, client, now=NOW, mailer_factory=lambda _: Mailer(messages))
    assert result["selected_unique"] == expected
    assert sum(result["selected_by_type"].values()) == expected
    assert len(messages) == 1
