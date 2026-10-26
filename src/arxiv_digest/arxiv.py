from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit
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


class AnnouncementGapError(ServiceError):
    """A checkpoint cannot be recovered from verifiable dated announcements."""

    recovery_required = True


def normalize_id(value: str) -> str:
    value = unquote(value.strip())
    if "://" in value:
        value = urlsplit(value).path.removeprefix("/abs/")
    value = re.sub(r"v\d+$", "", value.removeprefix("arXiv:"))
    if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})", value):
        raise ServiceError("arXiv: invalid paper identifier in feed")
    return value


def version_suffix(value: str) -> str:
    """Return a version only when the source explicitly supplies one."""
    match = re.search(r"(v\d+)$", unquote(value.strip()))
    return match[1] if match else ""


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
            version=version_suffix(raw_id),
            updated=entry.findtext("a:updated", default="", namespaces=NS),
        )
        if not paper.title or not paper.abstract or not paper.published or not all(categories):
            raise ServiceError(
                "arXiv: entry missing title, abstract, publication date, or category"
            )
        if not categories:
            raise ServiceError("arXiv: entry missing category")
        if not paper.authors or not all(paper.authors):
            raise ServiceError("arXiv: entry missing author names")
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
            version=version_suffix(entry.findtext("link", default="")),
            updated=entry.findtext("a:updated", default="", namespaces=NS),
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


NEW_SECTIONS = {
    "new submissions": "new",
    "cross submissions": "cross",
    "replacement submissions": "replace",
}


def parse_new_page(text: str) -> tuple[str, list[tuple[str, list[str], int]], int]:
    """Read /new's dated three-section page, including partial pagination slices."""
    soup = BeautifulSoup(text, "html.parser")
    headings = soup.find_all("h3")
    date_match = next(
        (
            match
            for heading in headings
            if (
                match := re.search(
                    r"Showing new listings for ([A-Za-z]+, \d{1,2} [A-Za-z]+ \d{4})",
                    heading.get_text(" ", strip=True),
                )
            )
        ),
        None,
    )
    if not date_match:
        raise ServiceError("arXiv /new: missing announcement date")
    try:
        announced = datetime.strptime(date_match[1], "%A, %d %B %Y").date().isoformat()
    except ValueError:
        raise ServiceError("arXiv /new: invalid announcement date") from None
    total_match = re.search(r"\bTotal of\s+([\d,]+)\s+entries\b", soup.get_text(" "))
    if not total_match:
        raise ServiceError("arXiv /new: missing total entry count")
    total = int(total_match[1].replace(",", ""))
    groups = []
    for heading in headings:
        label = heading.get_text(" ", strip=True)
        match = re.fullmatch(
            r"(New submissions|Cross submissions|Replacement submissions) "
            r"\(showing ([\d,]+) of ([\d,]+) entries\s*\)",
            label,
            re.I,
        )
        if not match:
            continue
        ids = []
        for element in heading.next_elements:
            if getattr(element, "name", None) == "h3":
                break
            if getattr(element, "name", None) == "dt":
                anchor = element.select_one('a[title="Abstract"]')
                if anchor is not None:
                    ids.append(normalize_id(urljoin("https://arxiv.org", anchor["href"])))
        shown, expected = (int(v.replace(",", "")) for v in match.groups()[1:])
        if len(ids) != shown or shown > expected or len(set(ids)) != len(ids):
            raise ServiceError("arXiv /new: truncated section or duplicate IDs")
        groups.append((NEW_SECTIONS[match[1].casefold()], ids, expected))
    if not groups and total:
        raise ServiceError("arXiv /new: missing submission sections")
    if len(soup.select('dt a[title="Abstract"]')) != sum(len(ids) for _, ids, _ in groups):
        raise ServiceError("arXiv /new: unrecognized submission section")
    return announced, groups, total


def fetch_new(settings: ArxivSettings, client: httpx.Client, limiter: RateLimiter) -> list[Paper]:
    discovered = {}
    for category in settings.categories:
        offset, total, announced = 0, None, None
        section_ids, section_totals, seen = {}, {}, set()
        while True:
            response = request(
                client,
                "GET",
                f"https://arxiv.org/list/{category}/new",
                service="arXiv new",
                limiter=limiter,
                params={"skip": offset, "show": 2000},
            )
            day, groups, count = parse_new_page(response.text)
            if count > 30000:
                raise ServiceError("arXiv /new: announcement exceeds the 30000-entry scan limit")
            if total is not None and (total != count or announced != day):
                raise ServiceError("arXiv /new: batch changed during pagination; rerun")
            total, announced = count, day
            page_count = 0
            for kind, ids, expected in groups:
                if kind in section_totals and section_totals[kind] != expected:
                    raise ServiceError("arXiv /new: section changed during pagination; rerun")
                if seen.intersection(ids):
                    raise ServiceError("arXiv /new: repeated IDs during pagination")
                section_totals[kind] = expected
                section_ids.setdefault(kind, []).extend(ids)
                seen.update(ids)
                page_count += len(ids)
            if offset + page_count > total or (not page_count and offset < total):
                raise ServiceError("arXiv /new: invalid pagination count")
            offset += page_count
            if offset == total:
                break
        if any(len(section_ids[kind]) != expected for kind, expected in section_totals.items()):
            raise ServiceError("arXiv /new: incomplete submission section")
        for kind, ids in section_ids.items():
            if kind == "cross" and not settings.include_cross_lists:
                continue
            for id in ids:
                previous = discovered.get(id)
                # One paper can be listed in several subscribed categories.
                priority = {"new": 0, "cross": 1, "replace": 2}
                if previous and previous[0] != announced:
                    raise ServiceError("arXiv /new: category announcement dates disagree")
                if not previous or priority[kind] < priority[previous[1]]:
                    discovered[id] = (announced, kind)
    if len(discovered) > settings.max_candidates:
        raise ServiceError("arXiv /new: candidate limit exceeded")
    papers = []
    for paper in fetch_by_ids(settings, client, list(discovered), limiter):
        day, kind = discovered[paper.id]
        if not settings.include_cross_lists and not any(
            category_matches(paper.categories[0], category) for category in settings.categories
        ):
            continue
        if kind == "replace" and not paper.version:
            raise ServiceError("arXiv /new: replacement metadata has no explicit version")
        papers.append(replace(paper, announced=day, announcement_type=kind))
    return papers


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
        normalize_id(urljoin("https://arxiv.org", a["href"]))
        for a in listing.select('dt a[title="Abstract"]')
    ]
    if len(ids) != int(count_match[1]) or len(set(ids)) != len(ids):
        raise ServiceError("arXiv: latest batch is truncated or contains duplicate IDs")
    return date, ids


def parse_recent_page(text: str) -> tuple[list[tuple[str, list[str], int]], int]:
    """Parse dated groups, allowing a batch to span listing pages.

    Monthly listings deliberately are not accepted: arXiv exposes no exact
    daily announcement boundaries there, only in the dated recent listing.
    """
    soup = BeautifulSoup(text, "html.parser")
    total_match = re.search(r"\bTotal of\s+([\d,]+)\s+entries\b", soup.get_text(" "))
    if not total_match:
        raise ServiceError("arXiv: recent listing missing total entry count")
    total = int(total_match[1].replace(",", ""))
    groups = []
    for heading in soup.find_all("h3"):
        label = heading.get_text(" ", strip=True)
        date_match = re.search(r"[A-Z][a-z]{2}, \d{1,2} [A-Z][a-z]{2} \d{4}", label)
        if not date_match:
            continue
        count_match = re.search(r"showing\s+(\d+)\s+of\s+(\d+)\s+entries", label)
        if not count_match:
            raise ServiceError("arXiv: unrecognized recent announcement header")
        announced = datetime.strptime(date_match[0], "%a, %d %b %Y").date().isoformat()
        ids = []
        # A heading can be inside or before a <dl>; stop at the next date rather
        # than accidentally collecting a following day's entries.
        for element in heading.next_elements:
            if getattr(element, "name", None) == "h3":
                break
            if getattr(element, "name", None) == "dt":
                anchor = element.select_one('a[title="Abstract"]')
                if anchor is not None:
                    ids.append(normalize_id(urljoin("https://arxiv.org", anchor["href"])))
        shown, expected = map(int, count_match.groups())
        if len(ids) != shown or shown > expected or len(set(ids)) != len(ids):
            raise ServiceError("arXiv: recent page is truncated or contains duplicate IDs")
        groups.append((announced, ids, expected))
    if not groups or total <= 0:
        raise ServiceError("arXiv: recent listing has no verifiable dated announcements")
    return groups, total


def fetch_recent_announcements(
    settings: ArxivSettings, client: httpx.Client, category: str, limiter: RateLimiter
) -> dict[str, list[str]]:
    """Read the entire available recent history without silently clipping a day."""
    batches: dict[str, list[str]] = {}
    totals: dict[str, int] = {}
    seen = set()
    offset, expected_total = 0, None
    while True:
        response = request(
            client,
            "GET",
            f"https://arxiv.org/list/{category}/recent",
            service="arXiv recent catch-up",
            limiter=limiter,
            params={"skip": offset, "show": 2000},
        )
        groups, total = parse_recent_page(response.text)
        if total > 30000:
            raise ServiceError("arXiv: recent history exceeds the 30000-entry recovery limit")
        if expected_total is not None and expected_total != total:
            raise ServiceError("arXiv: recent result set changed during pagination; rerun")
        expected_total = total
        page_count = 0
        for announced, ids, count in groups:
            if announced in totals and totals[announced] != count:
                raise ServiceError("arXiv: announcement batch changed during pagination; rerun")
            totals[announced] = count
            if seen.intersection(ids):
                raise ServiceError("arXiv: duplicate IDs during recent pagination; rerun")
            seen.update(ids)
            batches.setdefault(announced, []).extend(ids)
            page_count += len(ids)
        if not page_count or offset + page_count > total:
            raise ServiceError("arXiv: invalid recent pagination entry count")
        offset += page_count
        if offset == total:
            break
    if any(len(ids) != totals[announced] for announced, ids in batches.items()):
        raise ServiceError("arXiv: recent announcement batch is truncated; refusing recovery")
    return batches


def catch_up_ids(
    settings: ArxivSettings, client: httpx.Client, category: str, limiter: RateLimiter, since: str
) -> dict[str, str]:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        raise ServiceError("arXiv: invalid announcement checkpoint; expected YYYY-MM-DD")
    try:
        checkpoint = date.fromisoformat(since)
    except ValueError:
        raise ServiceError("arXiv: invalid announcement checkpoint date") from None
    batches = fetch_recent_announcements(settings, client, category, limiter)
    newest, oldest = max(batches), min(batches)
    if since >= newest:
        # Keep the latest batch available for a new recipient. Delivery history,
        # not a source checkpoint, decides which individual papers are eligible.
        dates = [newest]
    else:
        if (date.fromisoformat(newest) - checkpoint).days > settings.catch_up_max_days:
            raise AnnouncementGapError(
                "arXiv: announcement gap exceeds catch_up_max_days; "
                "use an explicit API-window recovery before resetting the checkpoint"
            )
        if since < oldest:
            raise AnnouncementGapError(
                "arXiv: checkpoint predates available recent announcement history; "
                "exact daily catch-up cannot be verified. Recover with API mode explicitly "
                "before resetting the checkpoint; submission dates are not announcement dates"
            )
        dates = sorted(announced for announced in batches if announced > since)
    return {id: announced for announced in dates for id in batches[announced]}


def fetch_by_ids(
    settings: ArxivSettings,
    client: httpx.Client,
    ids: list[str],
    limiter: RateLimiter | None = None,
) -> list[Paper]:
    """Fetch complete API metadata, including explicit versions, for known IDs."""
    if len(ids) > settings.max_candidates:
        raise ServiceError("arXiv: requested IDs exceed max_candidates")
    ids = [normalize_id(id) + version_suffix(id) for id in ids]
    requested = {normalize_id(id): version_suffix(id) for id in ids}
    if len(requested) != len(ids):
        raise ServiceError("arXiv: duplicate requested paper IDs")
    limiter = limiter or RateLimiter(settings.request_interval_seconds)
    papers = []
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
        page, total, count = parse_atom(response.text)
        if (
            {p.id for p in page} != {normalize_id(id) for id in batch}
            or count != len(batch)
            or len(page) != len(batch)
            or total != len(batch)
        ):
            raise ServiceError("arXiv: missing or unexpected papers in batch metadata")
        if any(requested[p.id] and p.version != requested[p.id] for p in page):
            raise ServiceError("arXiv: requested paper version does not match returned metadata")
        papers.extend(page)
    return papers


def fetch_latest(
    settings: ArxivSettings, client: httpx.Client, limiter: RateLimiter, *, since: str | None = None
) -> list[Paper]:
    announced: dict[str, str] = {}
    for category in settings.categories:
        if getattr(settings, "catch_up", False) and since:
            for id, announced_date in catch_up_ids(
                settings, client, category, limiter, since
            ).items():
                announced[id] = max(announced_date, announced.get(id, ""))
            continue
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
    for paper in fetch_by_ids(settings, client, list(announced), limiter):
        if not settings.include_cross_lists and not any(
            category_matches(paper.categories[0], c) for c in settings.categories
        ):
            continue
        papers.append(replace(paper, announced=announced[paper.id]))
    return papers


def fetch_papers(
    settings: ArxivSettings,
    client: httpx.Client,
    *,
    now: datetime | None = None,
    since: str | None = None,
) -> list[Paper]:
    limiter = RateLimiter(settings.request_interval_seconds)
    if settings.source == "new":
        return fetch_new(settings, client, limiter)
    if settings.source == "latest":
        return fetch_latest(settings, client, limiter, since=since)
    if settings.source == "rss":
        url = "https://rss.arxiv.org/rss/" + "+".join(settings.categories)
        text = request(client, "GET", url, service="arXiv RSS", limiter=limiter).text
        papers = parse_rss(text, include_cross_lists=settings.include_cross_lists)
        if len(papers) > settings.max_candidates:
            raise ServiceError("arXiv: candidate limit exceeded; raise max_candidates")
        # RSS is an announcement/discovery feed: dc:creator may combine all
        # authors in one string and pubDate is not the original submission date.
        # Resolve its filtered IDs through the API before screening or reporting.
        return fetch_by_ids(settings, client, [paper.id for paper in papers], limiter)

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
