"""Source fallback regression cases; no network or model calls."""

from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from arxiv_digest.enrichment import (
    Enricher,
    abs_evidence,
    ensure_public_host,
    html_evidence,
    merge_details,
    public_url,
)
from arxiv_digest.http import ServiceError
from arxiv_digest.models import Affiliation, PaperDetails

PROJECT = "https://project.example/"


def source_details(paper, source, *, project=None, affiliations=()):
    return PaperDetails(
        project_url=project,
        project_source=source if project else None,
        affiliations=[
            Affiliation(institution=name, authors=authors, source=source)
            for name, authors in affiliations
        ],
        sources=[source],
    )


def abs_page(paper, *, project=None):
    comments = (
        f'<table><tr><td class="comments">Project page: '
        f'<a href="{project}">this https URL</a></td></tr></table>'
        if project
        else ""
    )
    return (
        f'<h1 class="title">{paper.title}</h1>'
        f'<blockquote class="abstract">{paper.abstract}</blockquote>{comments}'
    )


@pytest.fixture
def prepare(config, monkeypatch):
    clients = []

    def factory(paper, *, html=None, extracted=None, pdf=None):
        html = html or {}
        extracted = extracted or {}
        events = []

        def unexpected(request):
            raise AssertionError(f"No actual network calls expected: {request.url}")

        client = httpx.Client(transport=httpx.MockTransport(unexpected))
        clients.append(client)
        enricher = Enricher(config, client, None)

        def fetch_html(self, url):
            events.append(("fetch", url))
            value = html.get(url)
            if isinstance(value, Exception):
                raise value
            if value is None:
                raise ServiceError("Simulated unavailable source")
            return value, url

        def pdf_evidence(self, actual_paper):
            assert actual_paper.id == paper.id
            events.append(("pdf", actual_paper.pdf_url))
            if isinstance(pdf, Exception):
                raise pdf
            if pdf is None:
                raise ServiceError("Simulated unavailable PDF")
            return pdf

        def extract(self, actual_paper, text, urls, source):
            assert actual_paper.id == paper.id
            events.append(("extract", source))
            value = extracted.get(source, PaperDetails(sources=[source]))
            if isinstance(value, Exception):
                raise value
            return value.model_copy(deep=True)

        monkeypatch.setattr(Enricher, "fetch_html", fetch_html, raising=False)
        monkeypatch.setattr(Enricher, "pdf_evidence", pdf_evidence)
        monkeypatch.setattr(Enricher, "extract", extract)
        return enricher, events

    yield factory
    for client in clients:
        client.close()


def test_abs_comments_uses_surrounding_label_for_generic_url_anchor(paper):
    text, urls = abs_evidence(abs_page(paper, project=PROJECT))
    assert "Project page" in text
    assert PROJECT in urls


def test_dedicated_project_website_replaces_code_repository_without_losing_code(paper):
    repository = "https://github.com/team/paper"
    current = source_details(paper, paper.url, project=repository)
    incoming = source_details(paper, paper.pdf_url, project=PROJECT)
    result = merge_details(current, incoming)
    assert result.project_url == PROJECT
    assert result.project_source == paper.pdf_url
    assert any(repository in note for note in result.notes)
    assert current.project_url == repository


def test_abs_abstract_plain_url_is_project_candidate(paper):
    html = f'<blockquote class="abstract">Our code is available at {PROJECT}</blockquote>'
    _, urls = abs_evidence(html)
    assert PROJECT in urls


def test_html_unlabelled_main_link_is_kept_but_bibliography_is_excluded():
    text, urls = html_evidence(
        f'<article><p>Project page: <a href="{PROJECT}">website</a></p>'
        '<section class="ltx_bibliography">'
        '<a href="https://citation.example/">project</a></section></article>'
    )
    assert PROJECT in urls
    assert "https://citation.example/" not in urls
    assert "citation.example" not in text


def test_abs_project_and_html_affiliations_combine_without_pdf(paper, prepare):
    html_url = f"https://arxiv.org/html/{paper.id}"
    enricher, events = prepare(
        paper,
        html={
            paper.url: abs_page(paper, project=PROJECT),
            html_url: f"<article>{paper.title} Example University A. Author</article>",
        },
        extracted={
            paper.url: source_details(paper, paper.url, project=PROJECT),
            html_url: source_details(
                paper, html_url, affiliations=[("Example University", paper.authors)]
            ),
        },
    )
    result = enricher.enrich(paper)
    assert result.project_url == PROJECT
    assert result.project_source == paper.url
    assert result.affiliations[0].institution == "Example University"
    assert result.affiliations[0].source == html_url
    assert ("pdf", paper.pdf_url) not in events


def test_html_affiliations_do_not_stop_pdf_search_for_project(paper, prepare):
    html_url = f"https://arxiv.org/html/{paper.id}"
    enricher, events = prepare(
        paper,
        html={paper.url: abs_page(paper), html_url: "<article>Example University</article>"},
        pdf=(f"Our project is available at {PROJECT}", [PROJECT]),
        extracted={
            html_url: source_details(
                paper, html_url, affiliations=[("Example University", paper.authors)]
            ),
            paper.pdf_url: source_details(paper, paper.pdf_url, project=PROJECT),
        },
    )
    result = enricher.enrich(paper)
    assert ("pdf", paper.pdf_url) in events
    assert result.project_url == PROJECT
    assert result.project_source == paper.pdf_url
    assert result.affiliations[0].institution == "Example University"
    assert result.affiliations[0].source == html_url


def test_html_project_survives_pdf_affiliation_fallback(paper, prepare):
    html_url = f"https://arxiv.org/html/{paper.id}"
    enricher, _ = prepare(
        paper,
        html={
            paper.url: abs_page(paper),
            html_url: f'<article>Project: <a href="{PROJECT}">website</a></article>',
        },
        pdf=("A. Author, Example University", []),
        extracted={
            html_url: source_details(paper, html_url, project=PROJECT),
            paper.pdf_url: source_details(
                paper, paper.pdf_url, affiliations=[("Example University", paper.authors)]
            ),
        },
    )
    result = enricher.enrich(paper)
    assert result.project_url == PROJECT
    assert result.project_source == html_url
    assert result.affiliations[0].source == paper.pdf_url
    assert html_url in result.sources and paper.pdf_url in result.sources


def test_project_page_supplies_missing_affiliation_after_html_and_pdf(paper, prepare):
    html_url = f"https://arxiv.org/html/{paper.id}"
    enricher, events = prepare(
        paper,
        html={
            paper.url: abs_page(paper, project=PROJECT),
            html_url: f"<article>{paper.title}</article>",
            PROJECT: (
                f"<main><h1>{paper.title}</h1><p>A. Author: Example University</p>"
                f'<a href="{paper.url}">Paper</a></main>'
            ),
        },
        pdf=("No institution stated", []),
        extracted={
            paper.url: source_details(paper, paper.url, project=PROJECT),
            PROJECT: source_details(
                paper, PROJECT, affiliations=[("Example University", paper.authors)]
            ),
        },
    )
    result = enricher.enrich(paper)
    assert ("fetch", PROJECT) in events
    assert result.project_url == PROJECT
    assert result.affiliations[0].source == PROJECT
    assert result.affiliations[0].authors == paper.authors


def test_unrelated_project_page_cannot_supply_institutions(paper, prepare):
    enricher, events = prepare(
        paper,
        html={
            paper.url: abs_page(paper, project=PROJECT),
            PROJECT: "<main><h1>Different paper</h1><p>Famous University</p></main>",
        },
        pdf=("No affiliation", []),
        extracted={
            paper.url: source_details(paper, paper.url, project=PROJECT),
            PROJECT: source_details(
                paper, PROJECT, affiliations=[("Famous University", paper.authors)]
            ),
        },
    )
    result = enricher.enrich(paper)
    assert ("fetch", PROJECT) in events
    assert ("extract", PROJECT) not in events
    assert result.affiliations == []
    assert result.project_url == PROJECT


def test_unknown_author_correspondence_does_not_cause_extra_source_requests(paper, prepare):
    paper = replace(paper, authors=["A. Author", "B. Author", "C. Author"])
    url = paper.metadata_html_url
    enricher, events = prepare(
        paper,
        html={
            paper.url: abs_page(paper, project=PROJECT),
            url: "<article>Paper University</article>",
        },
        extracted={
            paper.url: source_details(paper, paper.url, project=PROJECT),
            url: source_details(paper, url, affiliations=[("Paper University", [])]),
        },
    )
    result = enricher.enrich(paper)
    assert [a.institution for a in result.affiliations] == ["Paper University"]
    assert ("pdf", paper.pdf_url) not in events
    assert ("fetch", PROJECT) not in events


def test_later_source_failures_do_not_erase_abs_project(paper, prepare):
    enricher, _ = prepare(
        paper,
        html={
            paper.url: abs_page(paper, project=PROJECT),
            PROJECT: ServiceError("Simulated project outage"),
        },
        pdf=ServiceError("Simulated PDF outage"),
        extracted={paper.url: source_details(paper, paper.url, project=PROJECT)},
    )
    result = enricher.enrich(paper)
    assert result.project_url == PROJECT
    assert result.project_source == paper.url
    assert paper.url in result.sources


def test_lost_html_author_markers_do_not_discard_explicit_institutions(paper, prepare):
    url = paper.metadata_html_url
    html = (
        '<article><span class="ltx_creator ltx_role_author">A. Author B. Author</span>'
        "Affiliation: Paper University</article>"
    )
    enricher, events = prepare(
        paper,
        html={paper.url: abs_page(paper, project=PROJECT), url: html},
        extracted={
            paper.url: source_details(paper, paper.url, project=PROJECT),
            url: source_details(paper, url, affiliations=[("Paper University", [])]),
        },
    )
    result = enricher.enrich(paper)
    assert [a.institution for a in result.affiliations] == ["Paper University"]
    assert result.affiliations[0].source == url
    assert ("pdf", paper.pdf_url) not in events


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://localhost/",
        "https://something.localhost/",
        "https://robot.local/",
        "https://robot.internal/",
        "https://user:password@project.example/",
    ],
)
def test_external_source_rejects_private_local_and_credential_urls(url):
    assert not public_url(url)


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["10.0.0.5"], ["8.8.8.8", "192.168.1.2"], []])
def test_dns_resolution_must_contain_only_public_addresses(monkeypatch, addresses):
    monkeypatch.setattr(
        "arxiv_digest.enrichment.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", (address, 0)) for address in addresses],
    )
    with pytest.raises(ValueError, match="resolve to public"):
        ensure_public_host("https://project.example/")


def test_external_source_redirect_cannot_access_private_network(config, monkeypatch):
    requested = []
    monkeypatch.setattr("arxiv_digest.enrichment.ensure_public_host", lambda _: None)

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://192.168.1.8/private"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="non-public"):
            Enricher(config, client, None).fetch_html(PROJECT)
    assert requested == [PROJECT]


def test_external_source_html_size_is_bounded(config, monkeypatch):
    monkeypatch.setattr("arxiv_digest.enrichment.ensure_public_host", lambda _: None)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=b"x" * (2 * 1024 * 1024 + 1), headers={"Content-Type": "text/html"}
            )
        )
    ) as client:
        with pytest.raises(ValueError, match="extraction limit"):
            Enricher(config, client, None).fetch_html(PROJECT)


def test_external_source_redirect_chain_is_bounded(config, monkeypatch):
    requested = []
    monkeypatch.setattr("arxiv_digest.enrichment.ensure_public_host", lambda _: None)

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": f"/redirect-{len(requested)}"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="Too many source redirects"):
            Enricher(config, client, None).fetch_html(PROJECT)
    assert len(requested) == 4


def scan_fake_pdf(config, paper, monkeypatch, pages):
    scanned = []

    class Page:
        def __init__(self, index, text):
            self.index, self.text = index, text

        def extract_text(self):
            scanned.append(self.index)
            return self.text

        def get(self, key, default=None):
            return [] if key == "/Annots" else default

    reader = SimpleNamespace(pages=[Page(index, text) for index, text in enumerate(pages, 1)])
    monkeypatch.setattr("arxiv_digest.enrichment.PdfReader", lambda _: reader)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"%PDF-fake"))
    ) as client:
        text, urls = Enricher(config, client, None).pdf_evidence(paper)
    return text, urls, scanned


def test_pdf_finds_plain_project_url_on_third_page_and_excludes_references(
    config, paper, monkeypatch
):
    text, urls, scanned = scan_fake_pdf(
        config,
        paper,
        monkeypatch,
        [
            "A. Author\nExample University",
            "Methods and experiments",
            f"We release the project at {PROJECT}\nReferences\nhttps://citation.example/",
            "Appendix project https://appendix.example/",
        ],
    )
    assert urls == [PROJECT]
    assert "PDF page 3 link" in text
    assert "citation.example" not in text
    assert scanned == [1, 2, 3, 4]


@pytest.mark.parametrize(("limit", "expected"), [(2, []), (3, [PROJECT])])
def test_pdf_scan_honors_configured_page_limit(config, paper, monkeypatch, limit, expected):
    config.enrichment.max_pdf_pages = limit
    _, urls, scanned = scan_fake_pdf(
        config,
        paper,
        monkeypatch,
        ["A. Author, Example University", "Method", f"Project website: {PROJECT}"],
    )
    assert urls == expected
    assert scanned == list(range(1, limit + 1))
