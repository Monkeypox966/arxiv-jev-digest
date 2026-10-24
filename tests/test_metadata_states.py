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
    html_mapping_unclear,
    merge_details,
    reconcile_pdf_affiliations,
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


def test_real_later_merged_author_shape_is_ambiguous(paper):
    paper = replace(paper, authors=["Yining Wang", "Yuchen Liu"])
    assert html_mapping_unclear(later_merged_author_html(), paper)


def test_overlapping_name_substrings_are_not_two_authors_in_one_block(paper):
    paper = replace(paper, authors=["Yu Li", "Mingyu Li"])
    html = (
        '<span class="ltx_creator ltx_role_author"><span class="ltx_personname">Mingyu Li</span>'
        '<span class="ltx_contact ltx_role_affiliation">Example University</span></span>'
    )
    assert not html_mapping_unclear(html, paper)


def test_ambiguous_html_mapping_is_discarded_not_left_when_pdf_omits_author(config, paper, prepare):
    config.enrichment.follow_project_page = False
    paper = replace(paper, authors=["Yining Wang", "Yuchen Liu"])
    html_url = paper.metadata_html_url
    enricher, events = prepare(
        paper,
        html={paper.url: abs_page(paper), html_url: later_merged_author_html()},
        pdf=("Yining Wang2, Yuchen Liu\n2 Unisound AI Technology Co., Ltd", []),
        extracted={
            html_url: source_details(
                paper,
                html_url,
                project=PROJECT,
                affiliations=[("Unisound AI Technology Co., Ltd", paper.authors)],
            ),
            paper.pdf_url: source_details(
                paper,
                paper.pdf_url,
                affiliations=[("Unisound AI Technology Co., Ltd", ["Yining Wang"])],
            ),
        },
    )
    result = enricher.enrich(paper)
    assert ("pdf", paper.pdf_url) in events
    assert result.project_url == PROJECT
    assert result.project_source == html_url
    assert result.affiliations[0].authors == ["Yining Wang"]
    assert result.affiliations[0].source == paper.pdf_url
    assert result.affiliation_status == "partial"
    assert result.completeness(paper)["missing_authors"] == ["Yuchen Liu"]
    assert result.needs_refresh(paper)
    assert any("HTML 作者机构标记不可靠" in note for note in result.notes)
    assert any("Discarded" in note for note in result.source_diagnostics[html_url])


def test_ambiguous_html_without_pdf_does_not_silently_supply_institutions(config, paper, prepare):
    config.enrichment.follow_project_page = False
    paper = replace(paper, authors=["Yining Wang", "Yuchen Liu"])
    html_url = paper.metadata_html_url
    enricher, _ = prepare(
        paper,
        html={paper.url: abs_page(paper), html_url: later_merged_author_html()},
        extracted={
            html_url: source_details(
                paper,
                html_url,
                project=PROJECT,
                affiliations=[("Unisound AI Technology Co., Ltd", paper.authors)],
            )
        },
    )
    result = enricher.enrich(paper)
    assert result.project_url == PROJECT
    assert result.affiliations == []
    assert result.affiliation_status == "unavailable"


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
    assert result.notes == ["作者机构由论文元数据自动提取；请以来源页的署名为准。"]


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


def numbered_details(paper, rows):
    return source_details(paper, paper.metadata_pdf_url, affiliations=rows)


def test_explicit_pdf_markers_restore_all_seven_beyondmimic_authors(paper):
    authors = [
        "Qiayuan Liao",
        "Takara E. Truong",
        "Xiaoyu Huang",
        "Yuman Gao",
        "Guy Tevet",
        "Koushil Sreenath",
        "C. Karen Liu",
    ]
    paper = replace(paper, authors=authors)
    header = (
        "PDF page 1:\nBeyondMimic\n"
        "Qiayuan Liao1∗†, Takara E. Truong2†, Xiaoyu Huang1†,\n"
        "Yuman Gao1, Guy Tevet2, Koushil Sreenath1‡, C. Karen Liu2‡\n"
        "1University of California, Berkeley, CA 94720, USA.\n"
        "2Stanford University, Stanford, CA 94305, USA."
    )
    value = numbered_details(
        paper,
        [
            ("University of California, Berkeley", [authors[0], authors[2]]),
            ("Stanford University", [authors[1]]),
        ],
    )
    result = reconcile_pdf_affiliations(value, paper, header, paper.metadata_pdf_url)
    assert result.affiliations[0].authors == [authors[i] for i in (0, 2, 3, 5)]
    assert result.affiliations[1].authors == [authors[i] for i in (1, 4, 6)]
    assert result.completeness(paper)["affiliation_coverage"] == 1
    assert all(a.source == paper.metadata_pdf_url for a in result.affiliations)
    assert len(value.affiliations[0].authors) == 2


def test_explicit_pdf_marker_list_preserves_li_yi_dual_affiliation(paper):
    paper = replace(paper, authors=["He Wang", "Li Yi"])
    header = (
        "PDF page 1:\nHe Wang 2,4,†,Li Yi 1,5,†\n"
        "1Tsinghua University2Galbot Inc. 3Shanghai Jiao Tong University"
        "4Peking University 5Shanghai Qi Zhi\nInstitute"
    )
    value = numbered_details(
        paper,
        [
            ("Tsinghua University", []),
            ("Galbot Inc.", ["He Wang"]),
            ("Peking University", ["He Wang"]),
            ("Shanghai Qi Zhi Institute", ["Li Yi"]),
        ],
    )
    result = reconcile_pdf_affiliations(value, paper, header, paper.metadata_pdf_url)
    mapping = {a.institution: a.authors for a in result.affiliations}
    assert mapping["Tsinghua University"] == mapping["Shanghai Qi Zhi Institute"] == ["Li Yi"]
    assert "Shanghai Jiao Tong University" not in mapping


def test_explicit_pdf_marker_reconciliation_does_not_inherit_for_unmarked_yuchen(paper):
    paper = replace(paper, authors=["Yining Wang", "Yuchen Liu", "Guanjun Liu"])
    header = (
        "PDF page 1:\nYining Wang2, Yuchen Liu, Guanjun Liu1\n"
        "1University of Science and Technology of China\n2Unisound AI Technology Co., Ltd"
    )
    value = numbered_details(
        paper,
        [
            ("University of Science and Technology of China", ["Guanjun Liu"]),
            ("Unisound AI Technology Co., Ltd", ["Yining Wang", "Yuchen Liu"]),
        ],
    )
    result = reconcile_pdf_affiliations(value, paper, header, paper.metadata_pdf_url)
    assert result.affiliations[1].authors == ["Yining Wang"]
    assert result.completeness(paper)["missing_authors"] == ["Yuchen Liu"]


def test_pdf_year_is_not_a_two_digit_marker_or_inherited_next_author_marker(paper):
    paper = replace(paper, authors=["Alice Author", "Bob Author", "Carol Author"])
    header = (
        "PDF page 1:\nAlice Author1, Bob Author2025, Carol Author\n"
        "1Alpha University20Beta University"
    )
    value = numbered_details(
        paper,
        [
            ("Alpha University", ["Carol Author"]),
            ("Beta University", ["Bob Author"]),
        ],
    )
    result = reconcile_pdf_affiliations(value, paper, header, paper.metadata_pdf_url)
    assert result.affiliations[0].authors == ["Alice Author"]
    assert result.affiliations[1].authors == []


@pytest.mark.parametrize(
    "institutions", ["1Alpha University1Beta University", "1Alpha University2Alpha University"]
)
def test_conflicting_pdf_institution_numbers_are_not_guessed(paper, institutions):
    paper = replace(paper, authors=["Alice Author", "Bob Author"])
    header = "PDF page 1:\nAlice Author1, Bob Author2\n" + institutions
    rows = [("Alpha University", paper.authors)]
    if "Beta" in institutions:
        rows.append(("Beta University", paper.authors))
    result = reconcile_pdf_affiliations(
        numbered_details(paper, rows), paper, header, paper.metadata_pdf_url
    )
    assert all(not a.authors for a in result.affiliations)


def test_duplicate_author_with_conflicting_markers_is_left_unmapped(paper):
    paper = replace(paper, authors=["Alice Author", "Bob Author"])
    header = (
        "PDF page 1:\nAlice Author1, Bob Author2, Alice Author2\n1Alpha University2Beta University"
    )
    result = reconcile_pdf_affiliations(
        numbered_details(paper, [("Alpha University", []), ("Beta University", paper.authors)]),
        paper,
        header,
        paper.metadata_pdf_url,
    )
    assert result.affiliations[0].authors == []
    assert result.affiliations[1].authors == ["Bob Author"]


def test_unknown_pdf_marker_does_not_borrow_a_known_institution(paper):
    paper = replace(paper, authors=["Alice Author", "Bob Author"])
    header = "PDF page 1:\nAlice Author7, Bob Author1\n1Alpha University2Beta University"
    result = reconcile_pdf_affiliations(
        numbered_details(paper, [("Alpha University", ["Alice Author"]), ("Beta University", [])]),
        paper,
        header,
        paper.metadata_pdf_url,
    )
    assert result.affiliations[0].authors == ["Bob Author"]
    assert all("Alice Author" not in a.authors for a in result.affiliations)
    assert any(
        '"unknown_markers": ["7"]' in note for note in result.source_diagnostics[paper.pdf_url]
    )


def test_pdf_reconciliation_is_first_page_only_and_does_not_change_project_data(paper):
    paper = replace(paper, authors=["Alice Author", "Bob Author"])
    value = numbered_details(paper, [("Alpha University", []), ("Beta University", [])])
    header = (
        "PDF page 1:\nAlice Author1, Bob Author2\nPDF page 2:\n1Alpha University2Beta University"
    )
    assert reconcile_pdf_affiliations(value, paper, header, paper.metadata_pdf_url) == value
    project_header = "Alice Author1, Bob Author2\n1Alpha University2Beta University"
    assert reconcile_pdf_affiliations(value, paper, project_header, PROJECT) == value


def test_pdf_reconciliation_does_not_add_an_entire_missing_institution(paper):
    paper = replace(paper, authors=["Alice Author", "Bob Author"])
    value = numbered_details(paper, [("Alpha University", [])])
    header = "PDF page 1:\nAlice Author1, Bob Author2\n1Alpha University2Beta University"
    result = reconcile_pdf_affiliations(value, paper, header, paper.metadata_pdf_url)
    assert len(result.affiliations) == 1
    assert result.affiliations[0].authors == ["Alice Author"]
    assert result.completeness(paper)["missing_authors"] == ["Bob Author"]
