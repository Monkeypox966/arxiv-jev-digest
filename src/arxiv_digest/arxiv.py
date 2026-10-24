from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit
from xml.etree.ElementTree import ParseError

import httpx
from bs4 import BeautifulSoup
from defusedxml import ElementTree as ET

from .config import ArxivSettings
from .http import RateLimiter, ServiceError, request
from .models import Paper

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "o": "http://a9.com/-/spec/opensearch/1.1/",
    "x": "http://arxiv.org/schemas/atom",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def normalize_id(value: str) -> str:
    value = unquote(value.strip())
    if "://" in value:
        value = urlsplit(value).path.removeprefix("/abs/")
    value = re.sub(r"v\d+$", "", value.removeprefix("arXiv:"))
    if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})", value):
        raise ServiceError("arXiv: invalid paper identifier in feed")
    return value


def clean(value: str) -> str:
    return " ".join(value.split())


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_html(value: str) -> str:
    parser = TextOnly()
    parser.feed(value)
    return clean(unescape(" ".join(parser.parts)))


def xml_root(text: str):
    try:
        return ET.fromstring(text)
    except ParseError:
        raise ServiceError("arXiv: malformed XML response") from None


def parse_atom(text: str) -> tuple[list[Paper], int, int]:
    root = xml_root(text)
    if root.tag != "{" + NS["a"] + "}feed":
        raise ServiceError("arXiv: expected an Atom feed")
    total_text = root.findtext("o:totalResults", namespaces=NS)
    if total_text is None:
        raise ServiceError("arXiv: missing totalResults; refusing an incomplete response")
    if not total_text.strip().isdigit():
        raise ServiceError("arXiv: invalid totalResults")
    papers = []
    entries = root.findall("a:entry", NS)
    for entry in entries:
        raw_id = entry.findtext("a:id", default="", namespaces=NS)
        if raw_id.endswith("/errors") or "api/errors" in raw_id:
            raise ServiceError("arXiv: query returned an API error entry")
        categories = [c.attrib.get("term", "") for c in entry.findall("a:category", NS)]
        primary = entry.find("x:primary_category", NS)
        if primary is not None and primary.attrib.get("term"):
            categories = list(dict.fromkeys([primary.attrib["term"], *categories]))
        paper = Paper(
            id=normalize_id(raw_id),
            title=clean(entry.findtext("a:title", default="", namespaces=NS)),
            abstract=clean(entry.findtext("a:summary", default="", namespaces=NS)),
            authors=[clean(a.text or "") for a in entry.findall("a:author/a:name", NS)],
            categories=categories,
            published=entry.findtext("a:published", default="", namespaces=NS),
        )
        if not paper.title or not paper.abstract or not paper.published or not all(categories):
            raise ServiceError(
                "arXiv: entry missing title, abstract, publication date, or category"
            )
        if not categories:
            raise ServiceError("arXiv: entry missing category")
        papers.append(paper)
    return papers, int(total_text), len(entries)


def parse_rss(text: str, *, include_cross_lists: bool) -> list[Paper]:
    root = xml_root(text)
    if root.tag != "rss" or root.find("channel") is None:
        raise ServiceError("arXiv: expected an RSS feed")
    papers = {}
    for entry in root.findall("channel/item"):
        kind = entry.findtext("x:announce_type", default="", namespaces=NS).strip()
        if kind not in {"new", "cross", "replace", "replace-cross"}:
            raise ServiceError("arXiv: missing or unknown announcement type")
        if kind.startswith("replace") or (kind == "cross" and not include_cross_lists):
            continue
        description = plain_html(entry.findtext("description", default=""))
        abstract = description.split("Abstract:", 1)[-1].strip()
        paper = Paper(
            id=normalize_id(entry.findtext("link", default="")),
            title=clean(entry.findtext("title", default="")),
            abstract=abstract,
            authors=[entry.findtext("dc:creator", default="", namespaces=NS)],
            categories=[c.text.strip() for c in entry.findall("category") if c.text],
            published=entry.findtext(
                "pubDate", default=root.findtext("channel/pubDate", default="")
            ),
        )
        if not paper.title or not abstract:
            raise ServiceError("arXiv: RSS entry missing title or abstract")
        if paper.published:
            try:
                paper = Paper(
                    **{
                        **paper.to_dict(),
                        "published": parsedate_to_datetime(paper.published).isoformat(),
                    }
                )
            except (ValueError, TypeError):
                pass
        papers[paper.id] = paper
    return list(papers.values())


def category_matches(category: str, allowed: str) -> bool:
    return category == allowed or category.startswith(allowed + ".")


def parse_latest_listing(text: str) -> tuple[str, list[str]]:
    soup = BeautifulSoup(text, "html.parser")
    heading = soup.find("h3")
    if heading is None:
        raise ServiceError("arXiv: latest listing missing an announcement date")
    label = heading.get_text(" ", strip=True)
    date_match = re.search(r"[A-Z][a-z]{2}, \d{1,2} [A-Z][a-z]{2} \d{4}", label)
    count_match = re.search(r"of\s+(\d+)\s+entries", label)
    if not date_match or not count_match:
        raise ServiceError("arXiv: unrecognized latest announcement header")
    date = datetime.strptime(date_match[0], "%a, %d %b %Y").date().isoformat()
    # arXiv places the date heading INSIDE its <dl>. find_next('dl') would
    # silently select the following day's papers.
    listing = heading.find_parent("dl") or heading.find_next("dl")
    if listing is None:
        raise ServiceError("arXiv: announcement list missing")
    ids = [
        normalize_id("https://arxiv.org" + a["href"])
        for a in listing.select('dt a[title="Abstract"]')
    ]
    if len(ids) != int(count_match[1]) or len(set(ids)) != len(ids):
        raise ServiceError("arXiv: latest batch is truncated or contains duplicate IDs")
    return date, ids


def fetch_latest(
    settings: ArxivSettings, client: httpx.Client, limiter: RateLimiter
) -> list[Paper]:
    announced: dict[str, str] = {}
    for category in settings.categories:
        response = request(
            client,
            "GET",
            f"https://arxiv.org/list/{category}/recent",
            service="arXiv latest",
            limiter=limiter,
            params={"skip": 0, "show": 2000},
        )
        date, ids = parse_latest_listing(response.text)
        for id in ids:
            announced[id] = max(date, announced.get(id, ""))
    if len(announced) > settings.max_candidates:
        raise ServiceError("arXiv: latest batch exceeds max_candidates")
    papers = []
    ids = list(announced)
    for start in range(0, len(ids), settings.page_size):
        batch = ids[start : start + settings.page_size]
        response = request(
            client,
            "GET",
            "https://export.arxiv.org/api/query",
            service="arXiv API",
            limiter=limiter,
            params={"id_list": ",".join(batch), "max_results": len(batch)},
        )
        page, _, _ = parse_atom(response.text)
        if {p.id for p in page} != set(batch) or len(page) != len(batch):
            raise ServiceError("arXiv: missing or unexpected papers in batch metadata")
        for paper in page:
            if not settings.include_cross_lists and not any(
                category_matches(paper.categories[0], c) for c in settings.categories
            ):
                continue
            papers.append(replace(paper, announced=announced[paper.id]))
    return papers


def fetch_papers(
    settings: ArxivSettings, client: httpx.Client, *, now: datetime | None = None
) -> list[Paper]:
    limiter = RateLimiter(settings.request_interval_seconds)
    if settings.source == "latest":
        return fetch_latest(settings, client, limiter)
    if settings.source == "rss":
        url = "https://rss.arxiv.org/rss/" + "+".join(settings.categories)
        text = request(client, "GET", url, service="arXiv RSS", limiter=limiter).text
        papers = parse_rss(text, include_cross_lists=settings.include_cross_lists)
        if len(papers) > settings.max_candidates:
            raise ServiceError("arXiv: candidate limit exceeded; raise max_candidates")
        return papers

    now = (now or datetime.now(UTC)).astimezone(UTC)
    # Submission time precedes public announcement. A rolling window covers that delay,
    # weekends, and several missed runs; cache + reviewed history prevent repeated bills.
    start_time = now - timedelta(days=settings.lookback_days)
    archives = {
        "cs",
        "math",
        "stat",
        "eess",
        "econ",
        "physics",
        "q-bio",
        "q-fin",
        "astro-ph",
        "cond-mat",
        "nlin",
    }
    cats = " OR ".join(
        f"cat:{cat}.*" if cat in archives else f"cat:{cat}" for cat in settings.categories
    )
    dates = f"submittedDate:[{start_time:%Y%m%d%H%M} TO {now:%Y%m%d%H%M}]"
    query = f"({cats}) AND {dates}"
    papers: dict[str, Paper] = {}
    offset = 0
    expected_total = None
    seen_ids: set[str] = set()
    while True:
        response = request(
            client,
            "GET",
            "https://export.arxiv.org/api/query",
            service="arXiv API",
            limiter=limiter,
            params={
                "search_query": query,
                "start": offset,
                "max_results": settings.page_size,
                "sortBy": "submittedDate",
                "sortOrder": "ascending",
            },
        )
        page, total, count = parse_atom(response.text)
        if total > settings.max_candidates:
            raise ServiceError(
                f"arXiv: {total} candidates exceeds max_candidates={settings.max_candidates}; "
                "narrow categories/window or raise the limit"
            )
        if expected_total is not None and total != expected_total:
            raise ServiceError("arXiv: result set changed during pagination; rerun to avoid gaps")
        expected_total = total
        if not count and offset < total:
            raise ServiceError("arXiv: empty page before the end of results; rerun later")
        for paper in page:
            if paper.id in seen_ids:
                raise ServiceError("arXiv: repeated entry during pagination; rerun to avoid gaps")
            seen_ids.add(paper.id)
            if not settings.include_cross_lists and not any(
                category_matches(paper.categories[0], cat) for cat in settings.categories
            ):
                continue
            papers[paper.id] = paper
        offset += count
        if offset >= total:
            break
    return list(papers.values())
