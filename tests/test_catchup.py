from datetime import date

import httpx
import pytest
from conftest import atom

from arxiv_digest.arxiv import fetch_by_ids, fetch_papers, parse_atom, parse_recent_page, parse_rss
from arxiv_digest.http import ServiceError


def recent_page(groups, *, total=None):
    """Groups can be a partial slice of a single dated announcement batch."""
    total = sum(expected for _, _, expected in groups) if total is None else total
    sections = []
    for day, ids, expected in groups:
        label = date.fromisoformat(day).strftime("%a, %d %b %Y")
        entries = "".join(f'<dt><a title="Abstract" href="/abs/{id}">{id}</a></dt>' for id in ids)
        sections.append(
            f'<dl id="articles"><h3>{label} (showing {len(ids)} of {expected} entries )</h3>'
            + entries
            + "</dl>"
        )
    return f"<p>Total of {total} entries</p>" + "".join(sections)


def catchup_settings(config, **changes):
    return config.arxiv.model_copy(
        update={
            "source": "latest",
            "categories": ["cs.RO"],
            "catch_up": True,
            "catch_up_max_days": 14,
            **changes,
        }
    )


def metadata(ids):
    return atom([id if "v" in id else id + "v1" for id in ids]).replace("cs.AI", "cs.RO")


def source_transport(pages, calls, *, incomplete_metadata=False):
    def handler(request):
        calls.append(request)
        if request.url.host == "arxiv.org":
            return httpx.Response(200, text=pages[int(request.url.params["skip"])])
        ids = request.url.params["id_list"].split(",")
        return httpx.Response(200, text=metadata(ids[:-1] if incomplete_metadata else ids))

    return httpx.MockTransport(handler)


def test_weekend_recovery_uses_real_announcements_not_submission_dates(config):
    page = recent_page(
        [
            ("2026-10-19", ["2610.00001", "2610.00002"], 2),
            ("2026-10-16", ["2610.99999"], 1),
        ]
    )
    calls = []
    with httpx.Client(transport=source_transport({0: page}, calls)) as client:
        papers = fetch_papers(catchup_settings(config), client, since="2026-10-16")
    assert {p.id for p in papers} == {"2610.00001", "2610.00002"}
    assert {p.announced for p in papers} == {"2026-10-19"}
    assert all(p.version == "v1" for p in papers)
    assert len(calls) == 2


def test_cross_month_catchup_within_recent_history(config):
    page = recent_page(
        [
            ("2026-10-02", ["2610.00002"], 1),
            ("2026-10-01", ["2610.00001"], 1),
            ("2026-09-30", ["2609.99999"], 1),
        ]
    )
    with httpx.Client(transport=source_transport({0: page}, [])) as client:
        papers = fetch_papers(catchup_settings(config), client, since="2026-09-30")
    assert {p.id: p.announced for p in papers} == {
        "2610.00001": "2026-10-01",
        "2610.00002": "2026-10-02",
    }


def test_first_run_without_checkpoint_reads_latest_batch_only(config):
    page = recent_page([("2026-10-23", ["2610.00002"], 1), ("2026-10-22", ["2610.00001"], 1)])
    with httpx.Client(transport=source_transport({0: page}, [])) as client:
        papers = fetch_papers(catchup_settings(config), client)
    assert [p.id for p in papers] == ["2610.00002"]


@pytest.mark.parametrize("since", ["2026-10-23", "2026-10-24"])
def test_no_new_batch_still_returns_latest_for_recipient_dedup(config, since):
    page = recent_page([("2026-10-23", ["2610.00002"], 1), ("2026-10-22", ["2610.00001"], 1)])
    with httpx.Client(transport=source_transport({0: page}, [])) as client:
        papers = fetch_papers(catchup_settings(config), client, since=since)
    assert [p.id for p in papers] == ["2610.00002"]


def test_recent_listing_paginated_across_a_split_announcement_batch(config):
    pages = {
        0: recent_page([("2026-10-23", ["2610.00001", "2610.00002"], 3)], total=4),
        2: recent_page(
            [("2026-10-23", ["2610.00003"], 3), ("2026-10-22", ["2610.99999"], 1)],
            total=4,
        ),
    }
    calls = []
    with httpx.Client(transport=source_transport(pages, calls)) as client:
        papers = fetch_papers(catchup_settings(config, page_size=2), client, since="2026-10-22")
    assert {p.id for p in papers} == {"2610.00001", "2610.00002", "2610.00003"}
    assert [r.url.params["skip"] for r in calls if r.url.host == "arxiv.org"] == ["0", "2"]
    assert len([r for r in calls if r.url.host == "export.arxiv.org"]) == 2


@pytest.mark.parametrize(
    "since,changes,match",
    [
        ("2026-10-20", {"catch_up_max_days": 1}, "exceeds catch_up_max_days"),
        ("2026-10-19", {}, "predates available recent"),
        ("2026-02-30", {}, "invalid announcement checkpoint date"),
        ("20261022", {}, "expected YYYY-MM-DD"),
    ],
)
def test_unverifiable_checkpoint_fails_before_metadata_requests(config, since, changes, match):
    page = recent_page([("2026-10-23", ["2610.00002"], 1), ("2026-10-22", ["2610.00001"], 1)])
    calls = []
    with httpx.Client(transport=source_transport({0: page}, calls)) as client:
        with pytest.raises(ServiceError, match=match):
            fetch_papers(catchup_settings(config, **changes), client, since=since)
    assert not any(r.url.host == "export.arxiv.org" for r in calls)


@pytest.mark.parametrize("case", ["duplicate", "changed_total", "incomplete_batch"])
def test_recent_pagination_never_silently_drops_entries(config, case):
    first = recent_page([("2026-10-23", ["2610.00001"], 2)], total=2)
    second = recent_page([("2026-10-23", ["2610.00002"], 2)], total=2)
    if case == "duplicate":
        second = recent_page([("2026-10-23", ["2610.00001"], 2)], total=2)
    elif case == "changed_total":
        second = recent_page([("2026-10-23", ["2610.00002"], 2)], total=3)
    else:
        first = recent_page([("2026-10-23", ["2610.00001", "2610.00002"], 3)], total=2)
    with httpx.Client(transport=source_transport({0: first, 1: second}, [])) as client:
        with pytest.raises(ServiceError):
            fetch_papers(catchup_settings(config), client, since="2026-10-22")


def test_monthly_listing_without_dates_cannot_be_treated_as_daily_announcements():
    html = "<h2>October 2026</h2><p>Total of 1 entries</p><dl><dt>arXiv:2610.00001</dt></dl>"
    with pytest.raises(ServiceError, match="no verifiable dated announcements"):
        parse_recent_page(html)


def test_recent_page_header_count_must_match_actual_entries():
    page = recent_page([("2026-10-23", ["2610.00001"], 1)]).replace("showing 1", "showing 2")
    with pytest.raises(ServiceError, match="truncated"):
        parse_recent_page(page)


def test_catchup_api_metadata_must_be_complete(config):
    page = recent_page(
        [("2026-10-23", ["2610.00001", "2610.00002"], 2), ("2026-10-22", ["2610.99999"], 1)]
    )
    with httpx.Client(
        transport=source_transport({0: page}, [], incomplete_metadata=True)
    ) as client:
        with pytest.raises(ServiceError, match="missing or unexpected"):
            fetch_papers(catchup_settings(config), client, since="2026-10-22")


def test_fetch_by_ids_validates_explicit_version_and_keeps_updated(config):
    text = atom(["2610.00001v2"]).replace(
        "<author>", "<updated>2026-10-24T01:02:03Z</updated><author>"
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=text))
    ) as client:
        paper = fetch_by_ids(config.arxiv, client, ["2610.00001v2"])[0]
        assert paper.version == "v2"
        assert paper.updated == "2026-10-24T01:02:03Z"
        with pytest.raises(ServiceError, match="version does not match"):
            fetch_by_ids(config.arxiv, client, ["2610.00001v1"])


def test_fetch_by_ids_rejects_duplicate_base_ids_before_network(config):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("No request should be made"))
    ) as client:
        with pytest.raises(ServiceError, match="duplicate requested"):
            fetch_by_ids(config.arxiv, client, ["2610.00001v1", "2610.00001v2"])
        assert fetch_by_ids(config.arxiv, client, []) == []


def test_fetch_by_ids_canonicalizes_abs_urls_before_calling_api(config):
    def handler(request):
        assert request.url.params["id_list"] == "2610.00001v2"
        return httpx.Response(200, text=atom(["2610.00001v2"]))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        paper = fetch_by_ids(config.arxiv, client, ["https://arxiv.org/abs/2610.00001v2"])[0]
    assert paper.id == "2610.00001" and paper.version == "v2"


def test_version_and_updated_are_not_guessed_when_atom_omits_them():
    paper = parse_atom(atom(["2610.00001"]))[0][0]
    assert paper.version == paper.updated == ""


def test_rss_preserves_explicit_version_and_actual_updated():
    xml = (
        '<rss xmlns:arxiv="http://arxiv.org/schemas/atom" '
        'xmlns:atom="http://www.w3.org/2005/Atom"><channel><item>'
        "<link>https://arxiv.org/abs/2610.00001v3</link><title>Paper</title>"
        "<description>Abstract: The abstract.</description><category>cs.RO</category>"
        "<arxiv:announce_type>new</arxiv:announce_type>"
        "<atom:updated>2026-10-24T01:00:00Z</atom:updated></item></channel></rss>"
    )
    paper = parse_rss(xml, include_cross_lists=True)[0]
    assert paper.version == "v3"
    assert paper.updated == "2026-10-24T01:00:00Z"


@pytest.mark.parametrize("include_cross_lists", [False, True])
def test_rss_discovery_resolves_complete_api_authors_and_submission_metadata(
    config, include_cross_lists
):
    items = "".join(
        f"<item><link>https://arxiv.org/abs/{id}</link><title>RSS title</title>"
        "<description>Abstract: Announcement abstract.</description>"
        "<dc:creator>A. Author, B. Author</dc:creator>"
        "<pubDate>Fri, 23 Oct 2026 00:00:00 GMT</pubDate><category>cs.RO</category>"
        f"<arxiv:announce_type>{kind}</arxiv:announce_type></item>"
        for id, kind in (
            ("2610.00001", "new"),
            ("2610.00002", "cross"),
            ("2610.00003", "replace"),
        )
    )
    rss = (
        '<rss xmlns:arxiv="http://arxiv.org/schemas/atom" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>' + items + "</channel></rss>"
    )
    ids = ["2610.00001", "2610.00002"] if include_cross_lists else ["2610.00001"]
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "rss.arxiv.org":
            return httpx.Response(200, text=rss)
        assert request.url.host == "export.arxiv.org"
        assert request.url.params["id_list"].split(",") == ids
        api = atom([id + "v2" for id in ids]).replace(
            "<author><name>Researcher</name></author>",
            "<author><name>A. Author</name></author><author><name>B. Author</name></author>",
        )
        api = api.replace(
            "<published>2026-10-23T00:00:00Z</published>",
            "<published>2026-10-20T17:00:00Z</published><updated>2026-10-24T01:00:00Z</updated>",
        )
        return httpx.Response(200, text=api)

    settings = config.arxiv.model_copy(
        update={
            "source": "rss",
            "categories": ["cs.RO"],
            "include_cross_lists": include_cross_lists,
        }
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        papers = fetch_papers(settings, client)
    assert [p.id for p in papers] == ids
    assert len(requests) == 2
    assert all(p.authors == ["A. Author", "B. Author"] for p in papers)
    assert all(p.published == "2026-10-20T17:00:00Z" for p in papers)
    assert all(p.updated == "2026-10-24T01:00:00Z" and p.version == "v2" for p in papers)
    assert all(p.announced == "" for p in papers)


@pytest.mark.parametrize(
    "author_xml",
    [
        "",
        "<author><name/></author>",
        "<author><name>  \n  </name></author>",
        "<author><name>Valid Author</name></author><author><name/></author>",
    ],
)
def test_api_refuses_missing_or_blank_author_names(author_xml):
    api = atom(["2610.00001v1"]).replace("<author><name>Researcher</name></author>", author_xml)
    with pytest.raises(ServiceError, match="missing author names"):
        parse_atom(api)


def test_empty_rss_does_not_request_api_metadata(config):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "rss.arxiv.org"
        return httpx.Response(200, text="<rss><channel/></rss>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        papers = fetch_papers(config.arxiv.model_copy(update={"source": "rss"}), client)
    assert papers == []
    assert len(requests) == 1
