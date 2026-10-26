"""Deterministic, user-editable affiliation preference; never an inferred quality verdict."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from .config import RankingSettings
from .models import InstitutionAssessment, InstitutionMatch, Paper, PaperDetails, Ranking


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


@lru_cache(maxsize=4096)
def alias_pattern(alias: str) -> re.Pattern:
    alias = normalize(alias)
    # Bound ASCII abbreviations so MIT cannot match MITRE or Smith. Chinese
    # aliases can appear inside a longer Chinese affiliation without spaces.
    left = r"(?<![a-z0-9])" if alias[0].isascii() and alias[0].isalnum() else ""
    right = r"(?![a-z0-9])" if alias[-1].isascii() and alias[-1].isalnum() else ""
    return re.compile(left + re.escape(alias) + right)


def matches_alias(institution: str, alias: str) -> bool:
    return bool(alias_pattern(alias).search(normalize(institution)))


def company_identity(text: str) -> str:
    """Accept legal suffixes/known locations, never arbitrary research-unit suffixes."""
    parts = [part.strip() for part in text.split(",")]
    legal = r"(?:incorporated|inc|limited|ltd|llc|corporation|corp|co)\.?"
    locations = {
        "san francisco",
        "mountain view",
        "london",
        "usa",
        "u.s.a.",
        "us",
        "uk",
        "united states",
        "united states of america",
        "united kingdom",
        "california",
        "ca",
        "美国",
        "英国",
    }
    while len(parts) > 1 and (parts[-1] in locations or re.fullmatch(legal, parts[-1])):
        parts.pop()
    return re.sub(r"\s+" + legal + r"$", "", ",".join(parts)).strip()


def passes_institution_filter(ranking: Ranking, settings: RankingSettings) -> bool:
    if not settings.enabled:
        return True
    if ranking.status == "unknown":
        return settings.unknown_policy == "keep"
    if ranking.status == "unlisted":
        return settings.unlisted_policy == "keep"
    return ranking.institution_score >= settings.min_institution_score


def manual_matches(institution: str, settings: RankingSettings) -> list[InstitutionMatch]:
    matches, spans = [], []
    text = normalize(institution)
    # A parenthesized abbreviation belongs to the preceding full institution.
    # Never let NTU in 'National Taiwan University (NTU)' identify Nanyang.
    definitions = []
    for group in re.finditer(r"\(([a-z0-9&-]{2,10})\)", text):
        prefix = re.split(r"[;|]", text[: group.start()])[-1].strip()
        expanded = [
            (rule.name, len(normalize(alias)))
            for rule in settings.institutions
            for alias in rule.aliases
            if len(normalize(alias)) > 10 and matches_alias(prefix, alias)
        ]
        if expanded:
            longest = max(length for _, length in expanded)
            owners = {name for name, length in expanded if length == longest}
            definitions.append((group.start(1), group.end(1), owners))
        elif re.search(r"university|institute|college|academy|大学|学院|研究[所院]", prefix):
            definitions.append((group.start(1), group.end(1), set()))
    for rule in settings.institutions:
        found = []
        for alias in rule.aliases:
            if rule.match_mode == "exact" and company_identity(text) != company_identity(
                normalize(alias)
            ):
                continue
            occurrences = [
                occurrence
                for occurrence in alias_pattern(alias).finditer(text)
                if not any(
                    start <= occurrence.start()
                    and occurrence.end() <= end
                    and rule.name not in owners
                    for start, end, owners in definitions
                )
            ]
            if occurrences:
                found.append((alias, occurrences))
        if not found:
            continue
        alias = max((value for value, _ in found), key=lambda value: len(normalize(value)))
        matches.append(
            InstitutionMatch(institution=institution, rule=rule.name, alias=alias, score=rule.score)
        )
        spans.append([span for _, occurrences in found for span in occurrences])
    # A qualified campus/institute overrides the parent name nested inside it.
    # Distinct, non-overlapping institutions can still contribute independently.
    return [
        item
        for index, item in enumerate(matches)
        if any(
            not any(
                other.start() <= span.start()
                and span.end() <= other.end()
                and other.end() - other.start() > span.end() - span.start()
                for other_index, other_spans in enumerate(spans)
                if other_index != index
                for other in other_spans
            )
            for span in spans[index]
        )
    ]


def rank_paper(
    paper: Paper,
    relevance: float,
    details: PaperDetails | None,
    settings: RankingSettings,
    *,
    assessments: dict[str, InstitutionAssessment] | None = None,
) -> Ranking:
    matches, evaluated, scores = [], [], []
    affiliations = details.verified_affiliations() if details else []
    for affiliation in affiliations:
        fixed = manual_matches(affiliation.institution, settings)
        if fixed:
            # Explicit user preferences always override any cached model opinion.
            matches.extend(fixed)
            scores.extend(match.score for match in fixed)
            continue
        assessment = (assessments or {}).get(normalize(affiliation.institution))
        if assessment is not None:
            evaluated.append(assessment)
            scores.append(assessment.score)
        else:
            scores.append(settings.neutral_score)
    matches.sort(key=lambda match: (-match.score, match.rule, match.institution))
    institution_score = max(scores, default=settings.neutral_score)
    weight = settings.institution_weight if settings.enabled else 0.0
    return Ranking(
        relevance_score=relevance,
        institution_score=institution_score,
        institution_weight=weight,
        score=(1 - weight) * relevance + weight * institution_score,
        status=(
            "matched"
            if matches or any(item.status == "rule" for item in evaluated)
            else "assessed"
            if any(item.status == "assessed" for item in evaluated)
            else "unlisted"
            if affiliations
            else "unknown"
        ),
        matches=matches,
        assessments=evaluated,
    )
