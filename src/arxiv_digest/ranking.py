"""Deterministic, user-editable affiliation preference; never an inferred quality verdict."""

from __future__ import annotations

import re
import unicodedata

from .config import RankingSettings
from .models import InstitutionMatch, Paper, PaperDetails, Ranking


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def matches_alias(institution: str, alias: str) -> bool:
    text, alias = normalize(institution), normalize(alias)
    # Bound ASCII abbreviations so MIT cannot match MITRE or Smith. Chinese
    # aliases can appear inside a longer Chinese affiliation without spaces.
    left = r"(?<![a-z0-9])" if alias[0].isascii() and alias[0].isalnum() else ""
    right = r"(?![a-z0-9])" if alias[-1].isascii() and alias[-1].isalnum() else ""
    return bool(re.search(left + re.escape(alias) + right, text))


def passes_institution_filter(ranking: Ranking, settings: RankingSettings) -> bool:
    if not settings.enabled:
        return True
    if ranking.status == "unknown":
        return settings.unknown_policy == "keep"
    if ranking.status == "unlisted":
        return settings.unlisted_policy == "keep"
    return ranking.institution_score >= settings.min_institution_score


def rank_paper(
    paper: Paper, relevance: float, details: PaperDetails | None, settings: RankingSettings
) -> Ranking:
    matches = []
    has_verified_affiliation = False
    if details and details.sources:
        for affiliation in details.affiliations:
            # A named institution without an established author mapping is not
            # evidence that an author actually belongs to it.
            if not set(affiliation.authors).intersection(paper.authors):
                continue
            has_verified_affiliation = True
            for rule in settings.institutions:
                alias = next(
                    (
                        a
                        for a in rule.aliases
                        if (
                            normalize(affiliation.institution) == normalize(a)
                            if rule.match_mode == "exact"
                            else matches_alias(affiliation.institution, a)
                        )
                    ),
                    None,
                )
                if alias:
                    matches.append(
                        InstitutionMatch(
                            institution=affiliation.institution,
                            rule=rule.name,
                            alias=alias,
                            score=rule.score,
                        )
                    )
    matches.sort(key=lambda match: (-match.score, match.rule, match.institution))
    institution_score = max((match.score for match in matches), default=0.0)
    weight = settings.institution_weight if settings.enabled else 0.0
    return Ranking(
        relevance_score=relevance,
        institution_score=institution_score,
        institution_weight=weight,
        score=(1 - weight) * relevance + weight * institution_score,
        status="matched" if matches else "unlisted" if has_verified_affiliation else "unknown",
        matches=matches,
    )
