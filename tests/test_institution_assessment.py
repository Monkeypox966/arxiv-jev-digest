import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_pipeline import Mailer

from arxiv_digest.enrichment import Enricher
from arxiv_digest.http import ServiceError
from arxiv_digest.institution_assessment import InstitutionAssessor
from arxiv_digest.models import Affiliation, Decision, InstitutionAssessment, PaperDetails
from arxiv_digest.pipeline import run
from arxiv_digest.ranking import normalize, rank_paper
from arxiv_digest.store import Store

NOW = datetime(2026, 10, 26, 5, tzinfo=UTC)
WEBSITE = "https://research.example.edu/"
QUOTE = "Our robotics laboratory released open whole-body control methods and benchmarks."


class Model:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, 0

    def complete_json(self, system, context, parser, *, purpose):
        assert purpose == "institution_assessment"
        assert "untrusted" in system
        self.calls += 1
        if self.error:
            raise self.error
        return parser(json.dumps(self.result))


def model_result(**overrides):
    return {
        "institution": "Novel Robotics Institute",
        "score": 0.8,
        "confidence": 0.9,
        "reason": "Documented relevant research achievements.",
        "evidence": [{"source": WEBSITE, "quote": QUOTE}],
        **overrides,
    }


@pytest.fixture
def setup_assessor(config, monkeypatch):
    config.ranking.assess_unlisted = True
    lookups = []

    def resolve(_self, name):
        lookups.append(name)
        return "https://ror.org/test-org", "Novel Robotics Institute", WEBSITE

    monkeypatch.setattr(InstitutionAssessor, "resolve", resolve)
    monkeypatch.setattr(InstitutionAssessor, "evidence", lambda *_args: {WEBSITE: QUOTE})
    return lookups


def test_verified_assessment_is_cached_and_missing_names_get_neutral_score(
    config, paper, setup_assessor
):
    model = Model(model_result())
    with Store(config.state_dir) as store:
        assessor = InstitutionAssessor(config, None, model, store, now=NOW)
        result = assessor.assess("Novel Robotics Institute")
        assert result.status == "assessed" and result.score == 0.8
        assert result.sources == [WEBSITE]
        again = InstitutionAssessor(config, None, model, store, now=NOW + timedelta(days=1))
        config.ranking.institution_weight = 0.35
        config.ranking.assessment_limit_per_run += 1
        assert again.assess("Novel Robotics Institute").score == 0.8
        assert model.calls == len(setup_assessor) == 1
    settings = config.ranking
    metadata = PaperDetails(affiliations=[Affiliation(institution="Unlisted", source=WEBSITE)])
    assert rank_paper(paper, 0.9, metadata, settings).institution_score == 0.6
    assert rank_paper(paper, 0.9, None, settings).institution_score == 0.6


@pytest.mark.parametrize(
    "change",
    [
        {"score": 0.95},
        {"confidence": 0.3},
        {
            "evidence": [
                {"source": WEBSITE, "quote": "Invented research claims not in the source text."}
            ]
        },
        {"evidence": [{"source": "https://forged.example/", "quote": QUOTE}]},
    ],
)
def test_unsupported_or_low_confidence_results_cannot_promote(config, setup_assessor, change):
    with Store(config.state_dir) as store:
        assessor = InstitutionAssessor(config, None, Model(model_result(**change)), store, now=NOW)
        result = assessor.assess("Novel Robotics Institute")
    assert result.status == "neutral" and result.score == 0.6


def test_top_tier_needs_two_distinct_documents(config, setup_assessor):
    with Store(config.state_dir) as store:
        assessor = InstitutionAssessor(config, None, Model(), store, now=NOW)
        result = model_result(score=1)
        with pytest.raises(ValueError, match="two distinct"):
            assessor.parse(json.dumps(result), "Novel", "Novel", {WEBSITE: QUOTE})
        second = WEBSITE + "research"
        result["evidence"].append({"source": second, "quote": QUOTE})
        with pytest.raises(ValueError, match="two distinct"):
            assessor.parse(json.dumps(result), "Novel", "Novel", {WEBSITE: QUOTE, second: QUOTE})
        result["evidence"][1]["quote"] = (
            "A separate open robotics benchmark was published by the institute."
        )
        pages = {WEBSITE: QUOTE, second: result["evidence"][1]["quote"]}
        assert assessor.parse(json.dumps(result), "Novel", "Novel", pages).score == 1


def test_manual_preferences_override_a_cached_model_opinion(config, paper):
    config.ranking.enabled = True
    details = PaperDetails(
        affiliations=[Affiliation(institution="Tongji University", source=WEBSITE)]
    )
    opinions = {
        normalize("Tongji University"): InstitutionAssessment(
            institution="Tongji University", score=1, status="assessed", confidence=1
        )
    }
    result = rank_paper(paper, 0.9, details, config.ranking, assessments=opinions)
    assert result.institution_score == 0.8
    assert not result.assessments


def test_neutral_cache_expires_and_limit_does_not_permanently_cache_skips(config, setup_assessor):
    model = Model(model_result(confidence=0.2))
    config.ranking.assessment_limit_per_run = 1
    with Store(config.state_dir) as store:
        assessor = InstitutionAssessor(config, None, model, store, now=NOW)
        assert assessor.assess("Novel").score == 0.6
        assert assessor.assess("Second").status == "neutral"
        assert store.cached("institution", assessor.key("Second")) is None
        later = InstitutionAssessor(config, None, model, store, now=NOW + timedelta(days=8))
        later.assess("Novel")
        assert model.calls == 2


def test_access_or_budget_errors_are_not_hidden_as_neutral(config, setup_assessor):
    model = Model(error=ServiceError("Model budget exhausted", fatal=True))
    with Store(config.state_dir) as store:
        assessor = InstitutionAssessor(config, None, model, store, now=NOW)
        with pytest.raises(ServiceError, match="budget"):
            assessor.assess("Novel")


def test_registry_resolved_manual_name_needs_no_model_opinion(config, setup_assessor, monkeypatch):
    monkeypatch.setattr(
        InstitutionAssessor,
        "resolve",
        lambda *_a: ("https://ror.org/known", "Stanford University", WEBSITE),
    )
    model = Model(error=AssertionError("Manual preference must not call a model"))
    with Store(config.state_dir) as store:
        result = InstitutionAssessor(config, None, model, store, now=NOW).assess(
            "An affiliated lab"
        )
    assert result.status == "rule" and result.score == 1
    assert model.calls == 0


def test_redirected_third_party_page_is_not_treated_as_official(config, monkeypatch):
    with Store(config.state_dir) as store:
        assessor = InstitutionAssessor(config, None, Model(), store, now=NOW)
        monkeypatch.setattr(
            assessor.web,
            "fetch_html",
            lambda *_a: ("<main>" + QUOTE + "</main>", "https://unrelated.example/"),
        )
        assert assessor.evidence(WEBSITE) == {}


def test_registry_requires_a_unique_chosen_identity(config):
    payload = {"items": [{"chosen": False, "score": 1, "organization": {}}]}
    with httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=payload))
    ) as client:
        with Store(config.state_dir) as store:
            assessor = InstitutionAssessor(config, client, Model(), store, now=NOW)
            result = assessor.assess("Ambiguous company")
    assert result.status == "neutral"


def test_pipeline_assesses_unlisted_before_top_k_and_skips_manual_names(
    config, paper, summary, monkeypatch
):
    config.ranking.enabled = config.ranking.assess_unlisted = config.enrichment.enabled = True
    config.ranking.institution_weight = 0.35
    config.selection.top_k = 1
    other = replace(paper, id="2610.00002")
    calls = []
    monkeypatch.setattr("arxiv_digest.pipeline.fetch_papers", lambda *_a, **_k: [paper, other])
    monkeypatch.setattr(
        "arxiv_digest.clients.JevClient.judge",
        lambda _s, p: Decision(
            score=0.95 if p.id == paper.id else 0.9,
            interests={},
            exclusions={},
            excluded=False,
            model="test",
        ),
    )
    monkeypatch.setattr(
        Enricher,
        "enrich",
        lambda _s, p: PaperDetails(
            affiliations=[
                Affiliation(
                    institution="Tongji University" if p.id == paper.id else "Novel", source=WEBSITE
                )
            ]
        ),
    )
    monkeypatch.setattr("arxiv_digest.clients.DeepSeekClient.summarize", lambda *_a: summary)

    def assess(_self, name):
        calls.append(name)
        return InstitutionAssessment(institution=name, score=1, status="assessed", confidence=0.9)

    monkeypatch.setattr(InstitutionAssessor, "assess", assess)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _r: pytest.fail("Unexpected HTTP"))
    ) as client:
        result = run(config, client, now=NOW, mailer_factory=lambda _: Mailer([]))
    report = json.loads((Path(result["output_dir"]) / "digest.json").read_text("utf-8"))
    assert calls == ["Novel"]
    assert report["papers"][0]["paper"]["id"] == other.id
