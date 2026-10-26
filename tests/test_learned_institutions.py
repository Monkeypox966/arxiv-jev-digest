import json
from datetime import UTC, datetime, timedelta

import pytest

from arxiv_digest.config import InstitutionRule
from arxiv_digest.institution_assessment import InstitutionAssessor
from arxiv_digest.models import InstitutionAssessment
from arxiv_digest.ranking import normalize
from arxiv_digest.store import Store

NOW = datetime(2026, 10, 26, 8, tzinfo=UTC)
SITE = "https://river.example.edu/"
NAME = "River Robotics Institute"
QUOTE = "River Robotics Institute (RRI) publishes reproducible humanoid control benchmarks."


class Model:
    def __init__(self, *, confidence=0.95, score=0.8, forged=False, ambiguous=False):
        self.calls = []
        self.confidence, self.score = confidence, score
        self.forged, self.ambiguous = forged, ambiguous

    def complete_json(self, _system, context, parser, *, purpose):
        self.calls.append(purpose)
        if purpose == "institution_identity":
            return parser(
                json.dumps(
                    {
                        "canonical_name": NAME,
                        "website": SITE,
                        "confidence": self.confidence,
                        "ambiguous": self.ambiguous,
                        "evidence": [
                            {
                                "source": SITE,
                                "quote": "Forged evidence linking RRI to River Robotics Institute."
                                if self.forged
                                else QUOTE,
                            }
                        ]
                        if "pages" in context
                        else [],
                    }
                )
            )
        return parser(
            json.dumps(
                {
                    "institution": context["institution"],
                    "score": self.score,
                    "status": "assessed",
                    "confidence": self.confidence,
                    "reason": "Documented robotics research strength.",
                    "evidence": [{"source": SITE, "quote": QUOTE}],
                }
            )
        )


def fail(*_a, **_k):
    raise AssertionError("A learned institution must not require network or model calls")


@pytest.fixture
def official_pages(monkeypatch):
    monkeypatch.setattr(InstitutionAssessor, "evidence", lambda *_a: {SITE: QUOTE})
    monkeypatch.setattr(
        InstitutionAssessor, "resolve", lambda *_a: (_ for _ in ()).throw(ValueError("Not in ROR"))
    )


def test_model_resolves_missing_acronym_scores_and_persists_beyond_cache(
    config, official_pages, monkeypatch
):
    model = Model()
    with Store(config.state_dir) as store:
        initial = InstitutionAssessor(config, None, model, store, now=NOW).assess("RRI")
        assert initial.status == "assessed" and initial.score == 0.8
        assert initial.canonical_name == NAME
        assert initial.identity_evidence[0].quote == QUOTE
        assert model.calls == [
            "institution_identity",
            "institution_identity",
            "institution_assessment",
        ]
        store.prune(30, now=NOW + timedelta(days=60))
        assert store.db.execute("SELECT count(*) FROM cache").fetchone()[0] == 0
    monkeypatch.setattr(InstitutionAssessor, "resolve", fail)
    with Store(config.state_dir) as store:
        later = InstitutionAssessor(config, None, None, store, now=NOW + timedelta(days=60))
        assert later.assess("rri").score == 0.8
        assert later.assess(NAME).score == 0.8
        assert later.lookups == 0
        exported = store.export_institutions()
    assert exported["institutions"][0]["aliases"] == ["river robotics institute", "rri"]
    assert exported["institutions"][0]["identity_sources"] == [SITE]
    assert json.loads((config.state_dir / "institutions.json").read_text("utf-8")) == exported


def test_registry_resolved_alias_inherits_manual_score_and_updates_after_manual_edit(
    config, monkeypatch
):
    config.ranking.institutions = [InstitutionRule(name=NAME, score=0.8, aliases=[NAME])]
    monkeypatch.setattr(
        InstitutionAssessor, "resolve", lambda *_a: ("https://ror.org/river", NAME, SITE)
    )
    with Store(config.state_dir) as store:
        assessor = InstitutionAssessor(config, None, None, store, now=NOW)
        assert assessor.assess("RRI").score == 0.8
        config.ranking.institutions[0].score = 1.0
        monkeypatch.setattr(InstitutionAssessor, "resolve", fail)
        result = InstitutionAssessor(config, None, None, store, now=NOW).assess("RRI")
        assert result.status == "rule" and result.score == 1.0


def test_new_alias_reuses_canonical_score_without_regrading(config, official_pages, monkeypatch):
    model = Model()
    with Store(config.state_dir) as store:
        first = InstitutionAssessor(config, None, model, store, now=NOW)
        first.assess("RRI")
        monkeypatch.setattr(
            InstitutionAssessor, "resolve", lambda *_a: ("https://ror.org/river", NAME, SITE)
        )
        alias = InstitutionAssessor(config, None, None, store, now=NOW).assess("River RI")
        assert alias.score == 0.8
        assert len(model.calls) == 3
        monkeypatch.setattr(InstitutionAssessor, "resolve", fail)
        assert (
            InstitutionAssessor(config, None, None, store, now=NOW).assess("River RI").score == 0.8
        )
        assert "river ri" in store.export_institutions()["institutions"][0]["aliases"]


@pytest.mark.parametrize("option", ["forged", "ambiguous", "confidence"])
def test_unsupported_identity_cannot_enter_registry(config, official_pages, option):
    kwargs = {option: 0.3 if option == "confidence" else True}
    with Store(config.state_dir) as store:
        result = InstitutionAssessor(config, None, Model(**kwargs), store, now=NOW).assess("RRI")
        assert result.status == "neutral"
        assert store.export_institutions()["institutions"] == []


def test_known_b_tier_can_be_learned_but_insufficient_evidence_cannot(config, official_pages):
    with Store(config.state_dir) as store:
        result = InstitutionAssessor(config, None, Model(score=0.6), store, now=NOW).assess("RRI")
        assert result.status == "assessed" and result.score == 0.6
        assert len(store.export_institutions()["institutions"]) == 1
        with pytest.raises(ValueError, match="confirmed"):
            store.remember_institution(
                "scope", InstitutionAssessment(institution="Unknown", score=0.6), ["unknown"]
            )


def test_colliding_learned_alias_is_quarantined_instead_of_overwritten(config):
    with Store(config.state_dir) as store:
        assessor = InstitutionAssessor(config, None, None, store, now=NOW)
        for name in [NAME, "Other Robotics Institute"]:
            value = InstitutionAssessment(
                institution="RRI",
                canonical_name=name,
                score=0.8,
                status="assessed",
                confidence=0.95,
                sources=[SITE],
            )
            store.remember_institution(assessor.scope, value, ["rri", normalize(name)])
        assert assessor.assess("RRI").status == "neutral"
        assert len(store.export_institutions()["institutions"]) == 2


def test_automatic_scores_are_scoped_to_research_interests(config, official_pages):
    with Store(config.state_dir) as store:
        initial = InstitutionAssessor(config, None, Model(), store, now=NOW)
        initial.assess("RRI")
        config.interests[0].label = "A different research field"
        other = InstitutionAssessor(config, None, None, store, now=NOW)
        assert other.scope != initial.scope
        assert other.learned("RRI") is None


def test_learning_can_be_disabled_without_changing_manual_preferences(config, official_pages):
    config.ranking.learn_institutions = False
    with Store(config.state_dir) as store:
        result = InstitutionAssessor(config, None, Model(), store, now=NOW).assess("RRI")
        assert result.status == "assessed"
        assert store.export_institutions()["institutions"] == []


@pytest.mark.parametrize("collision", [False, True])
def test_registered_acronym_query_requires_unique_exact_name(config, collision):
    import httpx

    organization = {
        "id": "https://ror.org/river",
        "names": [
            {"value": NAME, "types": ["ror_display"]},
            {"value": "RRI", "types": ["acronym"]},
        ],
        "links": [{"type": "website", "value": SITE}],
    }
    calls = []

    def respond(req):
        calls.append(dict(req.url.params))
        items = [] if "affiliation" in req.url.params else [organization]
        if items and collision:
            items.append({**organization, "id": "https://ror.org/another"})
        return httpx.Response(200, json={"items": items})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with Store(config.state_dir) as store:
            assessor = InstitutionAssessor(config, client, None, store, now=NOW)
            if collision:
                with pytest.raises(ValueError, match="ambiguous"):
                    assessor.resolve("RRI")
            else:
                assert assessor.resolve("RRI") == (organization["id"], NAME, SITE)
    assert calls == [{"affiliation": "RRI"}, {"query": "RRI"}]


def test_neutral_assessment_without_quotes_is_valid_and_not_learned(config):
    with Store(config.state_dir) as store:
        assessor = InstitutionAssessor(config, None, None, store, now=NOW)
        raw = json.dumps(
            {
                "institution": "RRI",
                "score": 0.6,
                "status": "neutral",
                "confidence": 0.9,
                "reason": "Insufficient evidence.",
                "evidence": [],
            }
        )
        result = assessor.parse(raw, "RRI", NAME, {SITE: QUOTE})
        assessor.remember(result)
        assert result.status == "neutral" and result.canonical_name == NAME
        assert result.sources == [SITE]
        assert store.export_institutions()["institutions"] == []


def test_evidence_follows_institution_home_before_research_page(config, monkeypatch):
    pages = {
        SITE: '<main><a href="/university">River Robotics Institute</a>' + QUOTE + "</main>",
        SITE + "university": '<main><a href="/research">Research</a>' + QUOTE + "</main>",
        SITE + "research": "<main>" + QUOTE * 2 + "</main>",
    }
    calls = []

    def fetch(url):
        calls.append(url)
        return pages[url], url

    with Store(config.state_dir) as store:
        assessor = InstitutionAssessor(config, None, None, store, now=NOW)
        monkeypatch.setattr(assessor.web, "fetch_html", fetch)
        assert list(assessor.evidence(SITE, NAME)) == list(pages)
    assert calls == list(pages)


def test_explicit_review_preserves_paper_delivery_history(config, official_pages, monkeypatch):
    import httpx

    from arxiv_digest.operations import assess_institutions

    monkeypatch.setattr("arxiv_digest.operations.DeepSeekClient", lambda *_a, **_k: Model())
    with Store(config.state_dir) as store:
        delivery = store.prepare("recipient", "profile", ["paper"], ["paper"])
        store.resolve(delivery, "sent")
    with httpx.Client() as client:
        result = assess_institutions(config, client, ["RRI"])
    assert result["institutions_checked"] == result["learned_total"] == 1
    assert result["emails_sent"] == 0
    with Store(config.state_dir) as store:
        assert store.db.execute("SELECT count(*) FROM sent").fetchone()[0] == 1
        assert store.db.execute("SELECT count(*) FROM reviewed").fetchone()[0] == 1
        assert not store.pending()


@pytest.mark.parametrize("names", [[], [" "], [str(i) for i in range(6)]])
def test_explicit_review_rejects_invalid_names_before_any_calls(config, names):
    from arxiv_digest.operations import assess_institutions

    with pytest.raises(ValueError, match="1 to 5"):
        assess_institutions(config, None, names)
