import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_enrichment_fallbacks import PROJECT, abs_page, source_details
from test_enrichment_fallbacks import prepare as prepare
from test_metadata_states import later_merged_author_html
from test_pipeline import Mailer

from arxiv_digest.clients import profile_key
from arxiv_digest.enrichment import Enricher, checked_details, merge_details
from arxiv_digest.models import Affiliation, Decision, PaperDetails
from arxiv_digest.pipeline import run
from arxiv_digest.ranking import rank_paper
from arxiv_digest.store import Store, recipient_key

NOW = datetime(2026, 10, 26, 4, tzinfo=UTC)


@pytest.mark.parametrize("ignored_authors", [None, "unknown", ["Wrong Author"], {"bad": "mapping"}])
def test_irrelevant_author_field_cannot_block_institution_extraction(paper, ignored_authors):
    data = {"affiliations": [{"institution": " MIT ", "authors": ignored_authors}]}
    parsed = checked_details(json.dumps(data), paper, "Institutions: MIT", [])
    assert parsed.affiliations[0].institution == "MIT"
    assert parsed.affiliations[0].authors == []


@pytest.mark.parametrize("individual_source", [True, False])
def test_institution_scores_without_any_author_correspondence(config, paper, individual_source):
    details = PaperDetails(
        affiliations=[
            Affiliation(institution="MIT", source=paper.pdf_url if individual_source else None)
        ],
        sources=[] if individual_source else [paper.pdf_url],
    )
    config.ranking.enabled = True
    config.ranking.institution_weight = 0.35
    ranking = rank_paper(paper, 0.8, details, config.ranking)
    assert ranking.status == "matched"
    assert ranking.institution_score == 1
    assert ranking.score == pytest.approx(0.65 * 0.8 + 0.35)
    assert details.completeness(paper)["institutions_found"]


def test_names_still_need_verbatim_document_evidence(paper):
    data = {"affiliations": [{"institution": "MIT"}]}
    with pytest.raises(ValueError, match="verbatim"):
        checked_details(json.dumps(data), paper, "An unrelated document", [])
    assert (
        checked_details(json.dumps(data), paper, "Affiliations: MIT", []).affiliations[0].authors
        == []
    )


def test_merged_author_blocks_keep_institutions_and_skip_pdf(paper, prepare):
    url = paper.metadata_html_url
    enricher, events = prepare(
        paper,
        html={paper.url: abs_page(paper), url: later_merged_author_html()},
        extracted={
            url: source_details(
                paper, url, project=PROJECT, affiliations=[("Unisound AI Technology Co., Ltd", [])]
            )
        },
    )
    details = enricher.enrich(paper)
    assert details.affiliation_status == "found"
    assert details.completeness(paper)["institution_count"] == 1
    assert ("pdf", paper.pdf_url) not in events
    assert ("fetch", PROJECT) not in events


def test_merge_unions_and_deduplicates_institution_names_without_author_lists(paper):
    first = source_details(paper, paper.url, affiliations=[("MIT", [])])
    other = source_details(paper, paper.pdf_url, affiliations=[("mit", []), ("Stanford", [])])
    merged = merge_details(first, other, prefer_incoming=True)
    assert len(merged.affiliations) == 2
    assert {item.institution.casefold() for item in merged.affiliations} == {"mit", "stanford"}
    assert all(item.source == paper.pdf_url for item in merged.affiliations)
    assert first.affiliations[0].source == paper.url


def test_old_source_backed_cache_is_reused_without_mapping_retries(config, paper):
    enricher = Enricher(config, None, None)
    details = source_details(paper, paper.pdf_url, project=PROJECT, affiliations=[("MIT", [])])
    details.affiliation_status = "partial"
    with Store(config.state_dir) as store:
        store.cache(
            "details",
            enricher.key(paper, policy="details-v5-explicit-pdf-markers"),
            details.model_dump(),
            now=NOW - timedelta(days=2),
        )
        cached = enricher.cached_details(store, paper, now=NOW)
        assert cached.affiliation_status == "found"
        assert not cached.needs_refresh(paper)
        assert store.cached("details", enricher.key(paper)) is not None


def test_old_mapping_negative_does_not_block_new_extraction_or_override_new_cache(config, paper):
    enricher = Enricher(config, None, None)
    old_key = enricher.key(paper, policy="details-v5-explicit-pdf-markers")
    empty = source_details(paper, paper.pdf_url, project=PROJECT)
    with Store(config.state_dir) as store:
        store.cache("details", old_key, empty.model_dump(), now=NOW)
        assert enricher.cached_details(store, paper, now=NOW) is None
        positive = source_details(paper, paper.pdf_url, project=PROJECT, affiliations=[("MIT", [])])
        store.cache("details", old_key, positive.model_dump(), now=NOW)
        store.cache("details", enricher.key(paper), empty.model_dump(), now=NOW)
        assert not enricher.cached_details(store, paper, now=NOW).verified_affiliations()
        store.cache("details", enricher.key(paper), empty.model_dump(), now=NOW - timedelta(days=2))
        assert enricher.cached_details(store, paper, now=NOW) is None


def test_pipeline_scores_institution_list_and_clears_old_mapping_retry(
    config, paper, summary, monkeypatch
):
    config.enrichment.enabled = config.ranking.enabled = True
    metadata = source_details(paper, paper.pdf_url, project=PROJECT, affiliations=[("MIT", [])])
    reader = recipient_key("reader@example.com")
    with Store(config.state_dir) as store:
        store.queue_metadata_retry(
            reader,
            "old-mapping-profile",
            paper.to_dict(),
            delay_hours=24,
            max_attempts=3,
            now=NOW - timedelta(days=2),
        )
    monkeypatch.setattr("arxiv_digest.pipeline.fetch_papers", lambda *_args, **_kw: [paper])
    monkeypatch.setattr(
        "arxiv_digest.clients.JevClient.judge",
        lambda *_args: Decision(
            score=0.9, interests={}, exclusions={}, excluded=False, model="test"
        ),
    )
    monkeypatch.setattr(Enricher, "enrich", lambda *_args: metadata)
    monkeypatch.setattr("arxiv_digest.clients.DeepSeekClient.summarize", lambda *_args: summary)
    messages = []

    def unexpected(request):
        raise AssertionError(f"No real network expected: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        result = run(config, client, now=NOW, mailer_factory=lambda _: Mailer(messages))
    assert result["selected_unique"] == result["emails_sent"] == 1
    assert result["metadata_missing"] == 0
    with Store(config.state_dir) as store:
        assert store.db.execute("SELECT count(*) FROM metadata_retry").fetchone()[0] == 0
        assert not store.eligible(reader, profile_key(config), paper.id, now=NOW)
