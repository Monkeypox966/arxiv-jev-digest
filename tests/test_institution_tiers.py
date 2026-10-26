import pytest

from arxiv_digest.config import RankingSettings
from arxiv_digest.ranking import manual_matches


@pytest.mark.parametrize(
    ("institution", "expected"),
    [
        ("Beihang University", 0.8),
        ("BUAA", 0.8),
        ("北京航空航天大学", 0.8),
        ("Southern University of Science and Technology", 0.8),
        ("Hong Kong University of Science and Technology", 1),
        ("HKUST (Guangzhou)", 0.8),
        ("HKUST(GZ)", 0.8),
        ("香港科技大学广州校区", 0.8),
        ("香港科技大学（广州）", 0.8),
        ("Chinese University of Hong Kong", 1),
        ("CUHK-Shenzhen", 0.8),
        ("CUHK (SZ)", 0.8),
        ("The Chinese University of Hong Kong,Shenzhen", 0.8),
        ("The Chinese University of Hong Kong, Shenzhen", 0.8),
        ("中国科学院自动化研究所", 1),
        ("中国科学院计算技术研究所", 0.6),
        ("Institute of Computing Technology, Chinese Academy of Sciences", 0.6),
        ("Chinese Academy of Sciences, Institute of Software", 0.6),
        ("Chinese Academy of Sciences", 0.8),
        ("Harvard University", 0.6),
        ("UCLA", 0.6),
        ("Technical University of Munich", 1),
        ("KAIST", 1),
        ("NUS", 1),
        ("NTU", 1),
        ("RobotEra", 1),
        ("Galaxea", 1),
        ("AgiBot", 1),
        ("Agility Robotics", 1),
        ("Apptronik", 1),
        ("1X Technologies", 1),
        ("UBTECH", 1),
        ("AI2 Robotics", 0.8),
        ("Ai2", 1),
        ("IIT Delhi", 0.6),
        ("Indian Institute of Technology Delhi", 0.6),
        ("Italian Institute of Technology", 0.8),
        ("东北大学", 0.6),
        ("Tohoku University", 0.8),
        ("日本东北大学", 0.8),
        ("Physical Intelligence（π）", 1),
        ("Hong Kong University of Science and Technology (Guangzhou) (HKUST)", 0.8),
        ("The Chinese University of Hong Kong, Shenzhen (CUHK)", 0.8),
        ("Figure AI, Inc.", 1),
        ("Physical Intelligence, San Francisco, USA", 1),
        ("Humanoid, London, UK", 0.8),
        ("Intrinsic, Mountain View, USA", 0.8),
    ],
)
def test_supplied_institution_tiers_and_qualified_subunits(institution, expected):
    matches = manual_matches(institution, RankingSettings())
    assert max((match.score for match in matches), default=None) == expected


def test_explicit_parent_and_branch_collaboration_keeps_both_institutions():
    value = "CUHK and The Chinese University of Hong Kong, Shenzhen"
    matches = manual_matches(value, RankingSettings())
    assert {item.rule for item in matches} == {
        "Chinese University of Hong Kong",
        "Chinese University of Hong Kong, Shenzhen",
    }
    assert max(item.score for item in matches) == 1


@pytest.mark.parametrize(
    "institution",
    [
        "Humanoid Robotics Laboratory, Example University",
        "Intrinsic Motivation Laboratory, Example University",
        "Flexion Control Laboratory, Example University",
        "National Taiwan University (NTU)",
        "Physical Intelligence Lab, Example University",
        "Figure AI Research Laboratory, Example University",
        "Humanoid Robotics, London, UK",
    ],
)
def test_generic_research_words_are_not_confused_with_company_names(institution):
    assert not manual_matches(institution, RankingSettings())


def test_each_declared_alias_resolves_to_its_own_score_without_higher_score_collision():
    settings = RankingSettings()
    for rule in settings.institutions:
        for alias in (rule.name, *rule.aliases):
            matched = manual_matches(alias, settings)
            assert max((item.score for item in matched), default=None) == rule.score, (
                rule.name,
                alias,
            )
