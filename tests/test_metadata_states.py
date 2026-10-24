import json
from dataclasses import replace

import pytest
from test_enrichment_fallbacks import PROJECT, abs_page, scan_fake_pdf, source_details
from test_enrichment_fallbacks import prepare as prepare

from arxiv_digest.enrichment import (
    Enricher,
    abs_evidence,
    checked_details,
    classify_details,
    html_evidence,
)
from arxiv_digest.http import ServiceError
from arxiv_digest.models import Affiliation, PaperDetails


def test_metadata_urls_pin_revision_without_changing_public_links(paper):
    revised = replace(paper, version="v3", updated="2026-10-25T12:00:00Z")
    assert revised.url == paper.url
    assert revised.pdf_url == paper.pdf_url
    assert revised.metadata_url.endswith("2610.00001v3")
    assert revised.metadata_pdf_url.endswith("2610.00001v3")
    assert revised.metadata_html_url.endswith("2610.00001v3")
    assert replace(paper, version="3").versioned_id == revised.versioned_id
    with pytest.raises(ValueError, match="Invalid arXiv"):
        _ = replace(paper, version="v0").metadata_url


def test_completeness_counts_only_traceable_current_authors(paper):
    paper = replace(paper, authors=["A. Author", "B. Author"])
    value = PaperDetails(
        project_url=PROJECT,
        affiliations=[Affiliation(institution="Example", authors=["A. Author", "Unknown Author"])],
    )
    assert value.completeness(paper)["covered_authors"] == 0
    assert value.completeness(paper)["project_found"] is False
    value.affiliations[0].source = paper.pdf_url
    value.project_source = paper.url
    complete = value.completeness(paper)
    assert complete["covered_authors"] == 1
    assert complete["total_authors"] == 2
    assert complete["affiliation_coverage"] == 0.5
    assert complete["missing_authors"] == ["B. Author"]
    assert complete["project_found"] is True
    assert value.needs_refresh(paper)


def test_missing_project_needs_short_cache_even_after_successful_search(paper):
    value = source_details(paper, paper.pdf_url, affiliations=[("Example", paper.authors)])
    value = classify_details(value, paper, [], checked=True)
    assert value.affiliation_status == "found"
    assert value.project_status == "not_found"
    assert value.needs_refresh(paper)
    value.project_url = PROJECT
    assert not value.needs_refresh(paper)


def test_model_cannot_forge_verification_status_or_sources(paper):
    data = source_details(
        paper,
        "https://forged.example/",
        project=PROJECT,
        affiliations=[("Example University", paper.authors)],
    ).model_dump()
    data.update(
        project_status="found",
        affiliation_status="found",
        reviewed_on="2099-01-01",
        reviewed_version="v99",
        failed_sources=["https://forged.example/failure"],
    )
    value = checked_details(
        json.dumps(data), paper, "A. Author is with Example University.", [PROJECT]
    )
    assert value.project_status == value.affiliation_status == "unknown"
    assert value.sources == value.failed_sources == []
    assert value.project_source is None
    assert value.affiliations[0].source is None
    assert value.reviewed_on == value.reviewed_version == ""


def test_mapped_author_must_appear_in_supplied_document(paper):
    data = {"affiliations": [{"institution": "Example University", "authors": paper.authors}]}
    with pytest.raises(ValueError, match="Mapped author"):
        checked_details(json.dumps(data), paper, "Example University is a sponsor.", [])


def test_cache_key_tracks_revision_and_output_settings(config, paper):
    enricher = Enricher(config, None, None)
    original = enricher.key(paper)
    assert enricher.key(replace(paper, version="v2")) != original
    assert enricher.key(replace(paper, updated="2026-10-25T12:00:00Z")) != original
    config.deepseek.max_tokens += 1
    assert enricher.key(paper) != original
    another = enricher.key(paper)
    config.deepseek.disable_thinking = not config.deepseek.disable_thinking
    assert enricher.key(paper) != another


def test_successful_sources_mark_found_fields(paper, prepare):
    html_url = paper.metadata_html_url
    enricher, events = prepare(
        paper,
        html={paper.url: abs_page(paper, project=PROJECT), html_url: "<article>header</article>"},
        extracted={
            paper.url: source_details(paper, paper.url, project=PROJECT),
            html_url: source_details(paper, html_url, affiliations=[("Example", paper.authors)]),
        },
    )
    result = enricher.enrich(paper)
    assert result.project_status == result.affiliation_status == "found"
    assert not result.needs_refresh(paper)
    assert result.failed_sources == []
    assert ("pdf", paper.pdf_url) not in events


def test_all_sources_failing_is_unavailable_not_confirmed_absence(paper, prepare):
    enricher, _ = prepare(paper)
    result = enricher.enrich(paper)
    assert result.project_status == result.affiliation_status == "unavailable"
    assert set(result.failed_sources) == {paper.url, paper.metadata_html_url, paper.pdf_url}
    assert result.needs_refresh(paper)


def test_partial_author_mapping_is_not_complete(config, paper, prepare):
    config.enrichment.follow_project_page = False
    paper = replace(paper, authors=["A. Author", "B. Author"])
    enricher, _ = prepare(
        paper,
        html={
            paper.url: abs_page(paper, project=PROJECT),
            paper.metadata_html_url: "<article>x</article>",
        },
        pdf=("A. Author: Example", []),
        extracted={
            paper.url: source_details(paper, paper.url, project=PROJECT),
            paper.pdf_url: source_details(
                paper, paper.pdf_url, affiliations=[("Example", ["A. Author"])]
            ),
        },
    )
    result = enricher.enrich(paper)
    assert result.project_status == "found"
    assert result.affiliation_status == "partial"
    assert result.completeness(paper)["missing_authors"] == ["B. Author"]
    assert result.needs_refresh(paper)


def test_authentication_or_budget_failure_is_not_swallowed(paper, prepare):
    error = ServiceError("Model access denied or run budget exhausted")
    error.fatal = True
    enricher, _ = prepare(
        paper,
        html={paper.url: abs_page(paper, project=PROJECT)},
        extracted={paper.url: error},
    )
    with pytest.raises(ServiceError, match="budget exhausted"):
        enricher.enrich(paper)


def test_complete_fields_survive_a_nonfatal_source_failure(paper, prepare):
    html_url = paper.metadata_html_url
    enricher, _ = prepare(
        paper,
        html={paper.url: abs_page(paper, project=PROJECT), html_url: "<article>x</article>"},
        pdf=("A. Author: Example", []),
        extracted={
            paper.url: source_details(paper, paper.url, project=PROJECT),
            html_url: ServiceError("Invalid metadata JSON after retries"),
            paper.pdf_url: source_details(
                paper, paper.pdf_url, affiliations=[("Example", paper.authors)]
            ),
        },
    )
    result = enricher.enrich(paper)
    assert result.project_status == result.affiliation_status == "found"
    assert result.failed_sources == [html_url]
    assert not result.needs_refresh(paper)


def test_curated_record_requires_matching_known_revision(paper, prepare):
    paper = replace(paper, version="v2")
    enricher, events = prepare(
        paper,
        html={
            paper.metadata_url: abs_page(paper, project=PROJECT),
            paper.metadata_html_url: "<article>x</article>",
        },
        extracted={
            paper.metadata_url: source_details(paper, paper.metadata_url, project=PROJECT),
            paper.metadata_html_url: source_details(
                paper, paper.metadata_html_url, affiliations=[("New University", paper.authors)]
            ),
        },
    )
    curated = source_details(
        paper, paper.pdf_url, project=PROJECT, affiliations=[("Old University", paper.authors)]
    )
    curated.reviewed_version = "v1"
    enricher.verified[paper.id] = curated.model_dump()
    result = enricher.enrich(paper)
    assert ("fetch", paper.metadata_url) in events
    assert [a.institution for a in result.affiliations] == ["New University"]
    enricher.verified[paper.id]["reviewed_version"] = "v2"
    events.clear()
    result = enricher.enrich(paper)
    assert events == []
    assert result.affiliation_status == "found"
    assert [a.institution for a in result.affiliations] == ["Old University"]


def test_curated_partial_mapping_can_fill_missing_authors_without_overwriting(paper, prepare):
    paper = replace(paper, authors=["A. Author", "B. Author"])
    enricher, _ = prepare(
        paper,
        html={
            paper.url: abs_page(paper, project=PROJECT),
            paper.metadata_html_url: "<article>x</article>",
        },
        pdf=("A. Author B. Author", []),
        extracted={
            paper.url: source_details(paper, paper.url, project=PROJECT),
            paper.pdf_url: source_details(
                paper, paper.pdf_url, affiliations=[("New University", paper.authors)]
            ),
        },
    )
    enricher.verified[paper.id] = source_details(
        paper, paper.pdf_url, affiliations=[("Verified University", ["A. Author"])]
    ).model_dump()
    result = enricher.enrich(paper)
    assert result.affiliation_status == "found"
    assert result.project_status == "found"
    assert {a.institution: a.authors for a in result.affiliations} == {
        "Verified University": ["A. Author"],
        "New University": ["B. Author"],
    }


def test_pdf_resumes_at_clear_appendix_heading_but_ignores_reference_links(
    config, paper, monkeypatch
):
    text, urls, scanned = scan_fake_pdf(
        config,
        paper,
        monkeypatch,
        [
            "A. Author\nExample University",
            "References\nhttps://citation.example/",
            "More references\nhttps://other-citation.example/",
            f"Appendix A\nWe release our project at {PROJECT}",
        ],
    )
    assert scanned == [1, 2, 3, 4]
    assert urls == [PROJECT]
    assert "citation.example" not in text
    assert "PDF page 4 link" in text


def test_pdf_can_resume_after_references_on_same_page(config, paper, monkeypatch):
    text, urls, _ = scan_fake_pdf(
        config,
        paper,
        monkeypatch,
        [
            "Header",
            f"References\nhttps://citation.example/\nSupplementary Material\nProject: {PROJECT}",
        ],
    )
    assert urls == [PROJECT]
    assert "citation.example" not in text


def test_image_only_pdf_is_unavailable_instead_of_confirmed_absence(config, paper, monkeypatch):
    with pytest.raises(ValueError, match="OCR or manual"):
        scan_fake_pdf(config, paper, monkeypatch, ["", " "])


@pytest.mark.parametrize(
    ("parse", "html"),
    [
        (abs_evidence, "<html><h1>Service unavailable</h1></html>"),
        (html_evidence, '<main><div id="root"></div><script>renderApp()</script></main>'),
    ],
)
def test_empty_or_script_only_document_cannot_be_a_successful_negative(parse, html):
    with pytest.raises(ValueError, match="no readable metadata"):
        parse(html)
