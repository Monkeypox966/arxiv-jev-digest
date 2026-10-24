from datetime import UTC, datetime

import httpx
import pytest
from conftest import atom
from defusedxml.common import DefusedXmlException

from arxiv_digest.arxiv import fetch_papers, normalize_id, parse_atom, parse_rss
from arxiv_digest.http import ServiceError


@pytest.mark.parametrize(
    "value,expected",
    [
        ("http://arxiv.org/abs/2610.12345v3", "2610.12345"),
        ("arXiv:2610.1234v1", "2610.1234"),
        ("https://arxiv.org/abs/hep-th/9901001v2", "hep-th/9901001"),
    ],
)
def test_normalize_id(value, expected):
    assert normalize_id(value) == expected


@pytest.mark.parametrize("value", ["https://evil.test/paper", "../file", "", "2610.12345/script"])
def test_reject_invalid_ids(value):
    with pytest.raises(ServiceError):
        normalize_id(value)


def test_full_pagination_and_fixed_window(config):
    offsets = []
    queries = []

    def handler(request):
        offset = int(request.url.params["start"])
        offsets.append(offset)
        queries.append(request.url.params["search_query"])
        return httpx.Response(200, text=atom([f"2610.{offset + 1:05}v1"], total=3))

    config.arxiv.page_size = 1
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        papers = fetch_papers(config.arxiv, client, now=datetime(2026, 10, 24, tzinfo=UTC))
    assert offsets == [0, 1, 2]
    assert len(papers) == 3
    assert all("submittedDate:[202610170000 TO 202610240000]" in q for q in queries)


@pytest.mark.parametrize("case", ["over_limit", "empty_page", "changed_total"])
def test_refuse_incomplete_candidate_set(config, case):
    count = 0

    def handler(_):
        nonlocal count
        count += 1
        if case == "over_limit":
            return httpx.Response(200, text=atom(["2610.00001"], total=10001))
        if case == "empty_page":
            return httpx.Response(200, text=atom([], total=2))
        return httpx.Response(200, text=atom([f"2610.{count:05}"], total=count + 1))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ServiceError):
            fetch_papers(config.arxiv, client)


def test_atom_missing_metadata_fails():
    with pytest.raises(ServiceError):
        parse_atom('<feed xmlns="http://www.w3.org/2005/Atom"></feed>')


def rss_item(id, kind):
    return f"""<item><link>https://arxiv.org/abs/{id}</link><title>Paper</title>
    <description>&lt;p&gt;arXiv:{id} Announce Type: {kind}&lt;/p&gt;
    &lt;p&gt;Abstract: Actual abstract.&lt;/p&gt;</description>
    <arxiv:announce_type>{kind}</arxiv:announce_type><category>cs.AI</category></item>"""


def test_rss_dedup_and_skip_replacements():
    body = "".join(
        [
            rss_item("2610.00001v1", "new"),
            rss_item("2610.00001", "cross"),
            rss_item("2610.00002", "replace"),
            rss_item("2610.00003", "cross"),
        ]
    )
    xml = '<rss xmlns:arxiv="http://arxiv.org/schemas/atom"><channel>' + body + "</channel></rss>"
    papers = parse_rss(xml, include_cross_lists=True)
    assert [p.id for p in papers] == ["2610.00001", "2610.00003"]
    assert papers[0].abstract == "Actual abstract."
    assert len(parse_rss(xml, include_cross_lists=False)) == 1


def test_xml_external_entities_are_blocked():
    xml = '<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss>&x;</rss>'
    with pytest.raises(DefusedXmlException):
        parse_rss(xml, include_cross_lists=True)


def test_repeated_page_fails_instead_of_skipping_papers(config):
    def handler(_):
        return httpx.Response(200, text=atom(["2610.00001"], total=2))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ServiceError, match="repeated entry"):
            fetch_papers(config.arxiv, client)


def test_primary_category_is_used_for_crosslist_filter(config):
    text = atom(["2610.00001"]).replace(
        '<category term="cs.AI"/>',
        '<category term="cs.AI"/><category term="cs.CL"/>'
        '<primary_category xmlns="http://arxiv.org/schemas/atom" term="cs.CL"/>',
    )
    config.arxiv.categories = ["cs.AI"]
    config.arxiv.include_cross_lists = False
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=text))) as c:
        assert fetch_papers(config.arxiv, c) == []


def test_malformed_xml_gives_clear_service_error():
    with pytest.raises(ServiceError, match="malformed XML"):
        parse_atom("<feed")
