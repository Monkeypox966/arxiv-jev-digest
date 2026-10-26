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
    merge_details,
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


def test_completeness_counts_source_backed_institutions_without_author_mapping(paper):
    value = PaperDetails(project_url=PROJECT, affiliations=[Affiliation(institution="Example")])
    assert not value.completeness(paper)["institutions_found"]
    assert not value.completeness(paper)["project_found"]
    value.affiliations[0].source = paper.pdf_url
    value.project_source = paper.url
    complete = value.completeness(paper)
    assert complete["institutions_found"]
    assert complete["institution_count"] == 1
    assert complete["project_found"]
    assert not value.needs_refresh(paper)


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
        source_diagnostics={"https://forged.example/": ["This was independently verified"]},
    )
    value = checked_details(
        json.dumps(data), paper, "A. Author is with Example University.", [PROJECT]
    )
    assert value.project_status == value.affiliation_status == "unknown"
    assert value.sources == value.failed_sources == []
    assert value.project_source is None
    assert value.affiliations[0].source is None
    assert value.reviewed_on == value.reviewed_version == ""
    assert value.source_diagnostics == {}


def test_model_author_lists_are_ignored_and_not_required(paper):
    data = {"affiliations": [{"institution": "Example University", "authors": ["Unknown Person"]}]}
    result = checked_details(json.dumps(data), paper, "Example University", [])
    assert result.affiliations[0].institution == "Example University"
    assert result.affiliations[0].authors == []


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
        html={
            paper.url: abs_page(paper, project=PROJECT),
            html_url: "<article>header</article>",
            PROJECT: f"<article>{paper.title}</article>",
        },
        pdf=("Readable PDF header", []),
        extracted={
            paper.url: source_details(paper, paper.url, project=PROJECT),
            html_url: source_details(paper, html_url, affiliations=[("Example", paper.authors)]),
        },
    )
    result = enricher.enrich(paper)
    assert result.project_status == result.affiliation_status == "found"
    assert not result.needs_refresh(paper)
    assert result.failed_sources == []
    assert ("pdf", paper.pdf_url) in events


def test_all_sources_failing_is_unavailable_not_confirmed_absence(paper, prepare):
    enricher, _ = prepare(paper)
    result = enricher.enrich(paper)
    assert result.project_status == result.affiliation_status == "unavailable"
    assert set(result.failed_sources) == {paper.url, paper.metadata_html_url, paper.pdf_url}
    assert result.needs_refresh(paper)


def test_institutions_without_mappings_survive_failed_pdf_reconciliation(paper, prepare):
    paper = replace(paper, authors=["A. Author", "B. Author"])
    url = paper.metadata_html_url
    enricher, events = prepare(
        paper,
        html={
            paper.url: abs_page(paper, project=PROJECT),
            url: "<article>Example University</article>",
        },
        extracted={
            paper.url: source_details(paper, paper.url, project=PROJECT),
            url: source_details(paper, url, affiliations=[("Example University", [])]),
        },
    )
    result = enricher.enrich(paper)
    assert result.affiliation_status == "partial"
    assert result.needs_refresh(paper)
    assert ("pdf", paper.pdf_url) in events
    assert ("fetch", PROJECT) in events


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
    assert result.failed_sources == [html_url, PROJECT]
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


def test_curated_institution_does_not_require_author_completion(paper, prepare):
    enricher, events = prepare(
        paper,
        html={paper.url: abs_page(paper, project=PROJECT)},
        extracted={paper.url: source_details(paper, paper.url, project=PROJECT)},
    )
    enricher.verified[paper.id] = source_details(
        paper, paper.pdf_url, affiliations=[("Verified University", [])]
    ).model_dump()
    result = enricher.enrich(paper)
    assert result.affiliation_status == result.project_status == "found"
    assert [a.institution for a in result.affiliations] == ["Verified University"]
    assert ("pdf", paper.pdf_url) not in events
    assert ("fetch", PROJECT) not in events


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


def later_merged_author_html():
    # Shape observed in arXiv 2605.07315v1: one ltx_personname concatenates
    # Yining Wang and Yuchen Liu but carries one affiliation label.
    return (
        '<article><span class="ltx_creator ltx_role_author">'
        '<span class="ltx_personname">Yining WangYuchen Liu</span>'
        '<span class="ltx_author_notes"><span class="ltx_contact ltx_role_affiliation">'
        "Affiliation: Unisound AI Technology Co., Ltd"
        '</span></span></span><a href="https://project.example/">Project</a></article>'
    )


def test_model_notes_are_source_diagnostics_not_global_user_notes(config, paper):
    class Model:
        def complete_json(self, _system, _context, parser, **_kwargs):
            return parser(
                json.dumps(
                    {
                        "project_url": PROJECT,
                        "affiliations": [],
                        "notes": ["No affiliations appear in this source."],
                    }
                )
            )

    result = Enricher(config, None, Model()).extract(paper, "Header", [PROJECT], paper.url)
    assert result.source_diagnostics == {paper.url: ["No affiliations appear in this source."]}
    assert "No affiliations appear in this source." not in result.notes
    assert result.notes == ["署名机构由来源文档提取，不要求逐作者对应。"]


def test_merge_preserves_each_source_diagnostic_without_contradictory_global_notes(paper):
    current = source_details(paper, paper.url, project=PROJECT)
    current.notes = ["人工核实说明。"]
    current.source_diagnostics = {paper.url: ["No affiliations appear in this source."]}
    incoming = source_details(
        paper, paper.pdf_url, affiliations=[("Example University", paper.authors)]
    )
    incoming.source_diagnostics = {
        paper.pdf_url: ["No project URL appears in this PDF."],
        paper.url: ["No affiliations appear in this source.", "Abs comments inspected."],
    }
    result = classify_details(merge_details(current, incoming), paper, [], checked=True)
    assert result.project_status == result.affiliation_status == "found"
    assert result.notes == ["人工核实说明。"]
    assert result.source_diagnostics == {
        paper.url: ["No affiliations appear in this source.", "Abs comments inspected."],
        paper.pdf_url: ["No project URL appears in this PDF."],
    }
