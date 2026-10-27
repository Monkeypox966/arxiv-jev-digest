from datetime import UTC, datetime

import pytest
from conftest import ROOT
from pydantic import ValidationError

from arxiv_digest.cli import main
from arxiv_digest.config import Config, email_address, load_config, recipients
from arxiv_digest.models import Decision, Paper, Pick
from arxiv_digest.report import render
from arxiv_digest.store import Store


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c["selection"].update({"top_k": 0}),
        lambda c: c["selection"].update({"min_score": float("nan")}),
        lambda c: c["jev"].update({"endpoint": "http://unsafe.example/v1/systemone"}),
        lambda c: c["arxiv"].update({"categories": ["cs.AI OR all:*"]}),
        lambda c: c["deepseek"].update({"typo": True}),
        lambda c: c["interests"].append(c["interests"][0]),
        lambda c: c.update({"timezone": "not/a/zone"}),
    ],
)
def test_bad_configuration_rejected(config, mutation):
    data = config.model_dump()
    mutation(data)
    with pytest.raises(ValidationError):
        Config.model_validate(data)


@pytest.mark.parametrize(
    "value", ["a@example.com\r\nBcc: b@example.com", "Name <a@example.com>", "a,b@example.com"]
)
def test_header_injection_or_multiple_address_rejected(value):
    with pytest.raises(ValueError):
        email_address(value)


def test_env_recipients_override_and_dedup(config, monkeypatch):
    config.email.recipients = ["config@example.com"]
    monkeypatch.setenv("EMAIL_TO", "a@example.com;b@example.com,a@example.com")
    assert recipients(config) == ["a@example.com", "b@example.com"]


def test_report_escapes_untrusted_model_and_paper_text(config, paper, summary):
    paper = Paper(**{**paper.to_dict(), "title": '<img src="x" onerror="alert(1)">'})
    summary.tldr = "<script>alert('x')</script>"
    pick = Pick(
        paper,
        Decision(score=0.9, interests={}, exclusions={}, excluded=False, model="test"),
        summary,
    )
    _, text, html = render(config, [pick], datetime.now(UTC), {"fetched": 10})
    assert "<script>" not in html
    assert "<img src=" not in html
    assert "&lt;script&gt;" in html
    assert paper.url in text


def test_report_preserves_full_translated_abstract(config, paper, summary):
    summary.tldr = "原始摘要的完整译文。" * 180 + "\n\n结尾保留实验结果 42%。"
    pick = Pick(
        paper,
        Decision(score=0.9, interests={}, exclusions={}, excluded=False, model="test"),
        summary,
    )
    _, text, html = render(config, [pick], datetime.now(UTC), {"fetched": 1})
    assert summary.tldr in text and summary.tldr in html
    assert "white-space:pre-line" in html
    assert "原始摘要的完整译文" in text and "原始摘要的完整译文" in html


def test_state_persists_and_sent_history_survives_profile_change(tmp_path):
    with Store(tmp_path) as store:
        store.cache("decision", "key", {"score": 0.9})
        delivery = store.prepare("recipient-hash", "profile", ["paper-1", "paper-2"], ["paper-1"])
    with Store(tmp_path) as store:
        assert store.cached("decision", "key") == {"score": 0.9}
        store.resolve(delivery, "sent")
        assert not store.pending()
        assert not store.eligible("recipient-hash", "new-profile", "paper-1")
        assert store.eligible("recipient-hash", "new-profile", "paper-2")


def test_pruning_preserves_pending_and_sent_history(tmp_path):
    with Store(tmp_path) as store:
        store.cache("decision", "old", {"score": 0.9})
        store.db.execute("UPDATE cache SET created='2000-01-01'")
        delivery = store.prepare("recipient", "profile", ["p1"], ["p1"])
        store.resolve(delivery, "sent")
        store.prepare("other", "profile", ["p2"], ["p2"])
        store.prune(30)
        assert store.cached("decision", "old") is None
        assert not store.eligible("recipient", "new-profile", "p1")
        assert len(store.pending()) == 1


def test_example_check_and_offline_demo(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = tmp_path / "config.toml"
    path.write_text((ROOT / "config.toml").read_text(encoding="utf-8"), encoding="utf-8")
    assert main(["--config", str(path), "check"]) == 0
    assert main(["--config", str(path), "demo"]) == 0
    assert (tmp_path / "output/demo/digest.html").exists()
    assert not (tmp_path / ".state").exists()


def test_example_is_valid():
    cfg = load_config(ROOT / "config.toml")
    assert cfg.arxiv.categories and cfg.interests
