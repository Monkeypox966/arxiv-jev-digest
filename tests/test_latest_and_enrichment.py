import json
from datetime import UTC, datetime

import httpx
import pytest
from conftest import atom
from pydantic import ValidationError

from arxiv_digest.arxiv import fetch_papers, parse_latest_listing
from arxiv_digest.enrichment import Enricher, checked_details, html_evidence
from arxiv_digest.http import ServiceError
from arxiv_digest.models import Affiliation, Decision, PaperDetails, Pick
from arxiv_digest.report import render


def listing(ids=("2610.00001", "2610.00002"), total=2):
    entries = "".join(f'<dt><a title="Abstract" href="/abs/{id}">{id}</a></dt>' for id in ids)
    return (
        f'<dl id="articles"><h3>Fri, 23 Oct 2026 (showing {len(ids)} of {total} entries )</h3>'
        + entries
        + "</dl><dl><h3>Thu, 22 Oct 2026 (showing 1 of 1 entries )</h3>"
        '<dt><a title="Abstract" href="/abs/2610.99999">wrong day</a></dt></dl>'
    )


def test_announcement_header_inside_dl_does_not_select_next_day():
    date, ids = parse_latest_listing(listing())
    assert date == "2026-10-23"
    assert ids == ["2610.00001", "2610.00002"]


def test_truncated_latest_batch_fails_visibly():
    with pytest.raises(ServiceError, match="truncated"):
        parse_latest_listing(listing(total=69))


def test_latest_source_verifies_ids_and_keeps_announcement_date(config):
    config.arxiv.source = "latest"
    config.arxiv.categories = ["cs.RO"]
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.host == "arxiv.org":
            return httpx.Response(200, text=listing())
        assert request.url.params["id_list"] == "2610.00001,2610.00002"
        return httpx.Response(200, text=atom(["2610.00002v1", "2610.00001v1"]))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        papers = fetch_papers(config.arxiv, client)
    assert all(p.announced == "2026-10-23" for p in papers)
    assert paths == ["/list/cs.RO/recent", "/api/query"]


def test_latest_metadata_cannot_silently_drop_papers(config):
    config.arxiv.source = "latest"
    config.arxiv.categories = ["cs.RO"]
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, text=listing() if r.url.host == "arxiv.org" else atom(["2610.00001"])
            )
        )
    ) as client:
        with pytest.raises(ServiceError, match="missing or unexpected"):
            fetch_papers(config.arxiv, client)


def test_enrichment_never_accepts_an_invented_project_or_institution(paper):
    document = "A. Author is with Example University."
    valid = {
        "project_url": "https://project.example/paper",
        "affiliations": [{"institution": "Example University", "authors": ["A. Author"]}],
    }
    assert checked_details(json.dumps(valid), paper, document, [valid["project_url"]]).affiliations
    with pytest.raises(ValueError, match="Project URL"):
        checked_details(json.dumps(valid), paper, document, [])
    valid["project_url"] = None
    valid["affiliations"][0]["institution"] = "Imaginary Lab"
    with pytest.raises(ValueError, match="verbatim"):
        checked_details(json.dumps(valid), paper, document, [])
    valid["affiliations"][0] = {"institution": "Example University", "authors": ["Someone Else"]}
    with pytest.raises(ValueError, match="unknown author"):
        checked_details(json.dumps(valid), paper, document, [])


def test_bibliography_project_links_are_not_enrichment_candidates():
    text, urls = html_evidence(
        "<article><h1>Current paper</h1><p>Project: https://current.example/</p>"
        '<section class="ltx_bibliography"><a href="https://wrong.example/">Project</a>'
        "</section></article>"
    )
    assert "https://current.example/" in urls
    assert "https://wrong.example/" not in urls
    assert "wrong.example" not in text


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "data:text/html,x", "https://key:pass@host.test"]
)
def test_details_reject_unsafe_link_schemes(url):
    with pytest.raises(ValidationError):
        PaperDetails(project_url=url)


def test_verified_metadata_requires_no_network_or_model_call(config, paper):
    class NoModel:
        def complete_json(self, *_):
            raise AssertionError("Curated records must not call a model")

    paper = type(paper)(
        **{
            **paper.to_dict(),
            "id": "2610.31166",
            "authors": [
                "Chenyuan Chen",
                "Ran Ding",
                "Lechen Zhang",
                "Zewen He",
                "Yoshihiko Nakamura",
            ],
        }
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: (_ for _ in ()).throw(AssertionError("No HTTP expected"))
        )
    ) as client:
        details = Enricher(config, client, NoModel()).enrich(paper)
    assert details.reviewed_on == "2026-10-24"
    assert details.project_url == "https://memory-before-goal.github.io/"
    assert details.affiliations[0].authors == paper.authors


def test_compact_report_keeps_institution_overview_project_and_batch(config, paper, summary):
    config.compact_summaries = True
    details = PaperDetails(
        project_url="https://project.example/",
        affiliations=[Affiliation(institution="A < B University", authors=paper.authors)],
        sources=[paper.pdf_url],
    )
    pick = Pick(
        paper,
        Decision(score=0.9, interests={}, exclusions={}, excluded=False, model="test"),
        summary,
        details,
    )
    _, text, html = render(
        config, [pick], datetime.now(UTC), {"announcement_dates": ["2026-10-23"]}
    )
    assert "公告批次：2026-10-23" in text
    assert "Example" not in text
    assert "A < B University" in text
    assert "A &lt; B University" in html
    assert "https://project.example/" in html
    assert "研究问题</strong>" not in html
