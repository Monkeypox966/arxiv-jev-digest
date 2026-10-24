import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from conftest import atom, jev_response
from pydantic import ValidationError

from arxiv_digest.clients import decision_key, fingerprint, profile_key, summary_key
from arxiv_digest.config import Config, RankingSettings
from arxiv_digest.models import Affiliation, Decision, PaperDetails, Pick
from arxiv_digest.pipeline import run
from arxiv_digest.ranking import matches_alias, passes_institution_filter, rank_paper
from arxiv_digest.report import render


def details(paper, *institutions):
    return PaperDetails(
        affiliations=[
            Affiliation(institution=name, authors=paper.authors) for name in institutions
        ],
        sources=[paper.pdf_url],
    )


@pytest.mark.parametrize(
    ("institution", "alias", "expected"),
    [
        ("MIT CSAIL", "MIT", True),
        ("MITRE Corporation", "MIT", False),
        ("Smith Lab", "MIT", False),
        ("University of Washington", "Washington University", False),
        ("清华大学自动化系", "清华大学", True),
        ("ETH Zürich", "ETH Zürich", True),
        ("ＮＶＩＤＩＡ Research", "NVIDIA", True),
    ],
)
def test_affiliation_alias_boundaries_and_unicode(institution, alias, expected):
    assert matches_alias(institution, alias) is expected


def test_highest_institution_score_not_number_of_authors_or_affiliations(paper):
    settings = RankingSettings(enabled=True)
    single = rank_paper(paper, 0.9, details(paper, "MIT"), settings)
    multiple = rank_paper(paper, 0.9, details(paper, "MIT", "NVIDIA", "Stanford"), settings)
    assert single.score == multiple.score == pytest.approx(0.92)
    assert single.institution_score == multiple.institution_score == 1
    assert len(multiple.matches) == 3


def test_does_not_confuse_physical_intelligence_lab_with_pi_company(paper):
    settings = RankingSettings(enabled=True)
    lab = rank_paper(
        paper, 0.9, details(paper, "Physical Intelligence Lab, Tongji University"), settings
    )
    company = rank_paper(paper, 0.9, details(paper, "Physical Intelligence"), settings)
    assert lab.institution_score == 0.8
    assert [m.rule for m in lab.matches] == ["Tongji University"]
    assert company.institution_score == 1


@pytest.mark.parametrize("missing", ["sources", "authors", "wrong_author", "affiliations"])
def test_missing_evidence_is_unknown_and_not_guessed(paper, missing):
    value = details(paper, "MIT")
    if missing == "sources":
        value.sources = []
    elif missing == "authors":
        value.affiliations[0].authors = []
    elif missing == "wrong_author":
        value.affiliations[0].authors = ["Famous Person"]
    else:
        value.affiliations = []
    settings = RankingSettings(enabled=True)
    result = rank_paper(paper, 0.95, value, settings)
    assert result.status == "unknown"
    assert result.institution_score == 0
    assert passes_institution_filter(result, settings)
    settings.unknown_policy = "exclude"
    assert not passes_institution_filter(result, settings)


def test_verified_unlisted_and_lower_scores_are_filtered(paper):
    settings = RankingSettings(enabled=True)
    unlisted = rank_paper(paper, 0.99, details(paper, "Unlisted University"), settings)
    assert unlisted.status == "unlisted"
    assert passes_institution_filter(unlisted, settings)
    settings.unlisted_policy = "exclude"
    assert not passes_institution_filter(unlisted, settings)
    lower = rank_paper(paper, 0.99, details(paper, "ROBOTIS"), settings)
    assert passes_institution_filter(lower, settings)
    settings.min_institution_score = 0.8
    assert not passes_institution_filter(lower, settings)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c["ranking"].update(enabled=True),  # enrichment is disabled
        lambda c: c["ranking"].update(institution_weight=0.51),
        lambda c: c["ranking"].update(min_institution_score=float("nan")),
        lambda c: c["ranking"].update(unknown_policy="guess"),
        lambda c: c["ranking"].update(institutions=[{"name": "X", "score": 1, "aliases": [" "]}]),
    ],
)
def test_invalid_ranking_configuration(config, mutation):
    data = config.model_dump()
    mutation(data)
    with pytest.raises(ValidationError):
        Config.model_validate(data)


def test_ranking_changes_review_profile_but_preserves_existing_model_caches(config, paper):
    old_profile, old_decision, old_summary = (
        profile_key(config),
        decision_key(config, paper),
        summary_key(config, paper),
    )
    # Exact legacy cache key compatibility, including its nested fingerprint.
    assert old_decision == fingerprint(
        [old_profile, paper.id, paper.title, paper.abstract, paper.categories]
    )
    config.ranking.enabled = True
    assert profile_key(config) != old_profile
    assert decision_key(config, paper) == old_decision
    assert summary_key(config, paper) == old_summary
    profile = profile_key(config)
    config.ranking.min_institution_score = 0.8
    assert profile_key(config) != profile
    assert decision_key(config, paper) == old_decision


def test_report_explains_scores_and_escapes_institution_evidence(config, paper, summary):
    config.ranking.enabled = True
    value = details(paper, "MIT <script>")
    ranking = rank_paper(paper, 0.9, value, config.ranking)
    pick = Pick(
        paper,
        Decision(score=0.9, interests={}, exclusions={}, excluded=False, model="test"),
        summary,
        value,
        ranking,
    )
    _, text, html = render(config, [pick], datetime.now(UTC), {})
    assert "综合排序分：92.0/100" in text
    assert "机构偏好分：100/100" in text
    assert "MIT <script> →" in text
    assert "MIT &lt;script&gt;" in html
    assert "<script>" not in html
    assert "80%" in html and "20%" in html
    assert pick.to_dict()["ranking"]["matches"][0]["alias"] == "MIT"


def test_pipeline_relevance_gate_then_metadata_filter_then_ranking_and_top_k(
    config, summary, monkeypatch
):
    config.enrichment.enabled = config.ranking.enabled = True
    config.ranking.unlisted_policy = "exclude"
    config.selection.top_k = 1
    # 01: slightly more relevant but lower institution preference.
    # 02: initially below Top K, should win after institution ranking.
    # 03: famous institution but irrelevant. 04: excluded application.
    # 05: very relevant but a verified unlisted institution: filter before summary.
    ids = [f"2610.{i:05d}" for i in range(1, 6)]
    metadata_calls, summary_calls, jev_calls, messages = [], [], [], []

    def enrich(self, paper):
        metadata_calls.append(paper.id)
        institutions = {ids[0]: "ROBOTIS", ids[1]: "MIT", ids[4]: "Unlisted University"}
        return details(paper, institutions[paper.id])

    monkeypatch.setattr("arxiv_digest.enrichment.Enricher.enrich", enrich)

    def handler(request):
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, text=atom(ids))
        if request.url.host == "api.typesafe.ai":
            data = json.loads(request.content)
            id = data["state"]["title"].split()[-1]
            jev_calls.append(id)
            score = {ids[0]: 0.95, ids[1]: 0.9, ids[2]: 0.2, ids[3]: 0.99, ids[4]: 0.99}[id]
            return httpx.Response(
                200,
                json=jev_response(config, interest=score, exclusion=0.99 if id == ids[3] else 0),
            )
        raise AssertionError(f"Unexpected network endpoint: {request.url}")

    def summarize(self, paper):
        summary_calls.append(paper.id)
        return summary

    monkeypatch.setattr("arxiv_digest.clients.DeepSeekClient.summarize", summarize)
    from test_pipeline import Mailer

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = run(config, client, dry_run=True)
        first_report = json.loads((Path(first["output_dir"]) / "digest.json").read_text("utf-8"))
        audit = json.loads((Path(first["output_dir"]) / "ranking-audit.json").read_text("utf-8"))
        assert first_report["papers"][0]["paper"]["id"] == ids[1]
        assert set(metadata_calls) == {ids[0], ids[1], ids[4]}
        assert first["institution_filtered"] == 1
        assert summary_calls == [ids[1]]
        assert len(audit["papers"]) == 3
        assert not next(p for p in audit["papers"] if p["id"] == ids[4])[
            "passes_institution_filter"
        ]

        # A ranking change reuses decisions, metadata and summaries, then delivery
        # succeeds once. Another ranking change reconsiders the previously unsent
        # overflow, but cannot resend the already delivered paper.
        config.ranking.institution_weight = 0.3
        run(config, client, mailer_factory=lambda _: Mailer(messages))
        config.ranking.institution_weight = 0.4
        result = run(config, client, mailer_factory=lambda _: Mailer(messages))
        repeat = run(config, client, mailer_factory=lambda _: Mailer(messages))
    assert len(jev_calls) == 5
    assert len(metadata_calls) == 3
    assert summary_calls == [ids[1], ids[0]]
    assert len(messages) == 2
    assert result["emails_sent"] == 1
    assert repeat["emails_sent"] == 0
    assert ids[1] not in messages[1].get_body(preferencelist=("plain",)).get_content()
