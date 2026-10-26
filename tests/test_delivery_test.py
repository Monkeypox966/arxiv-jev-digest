import json
from pathlib import Path

import pytest

from arxiv_digest.http import ServiceError
from arxiv_digest.operations import test_delivery as send_test
from arxiv_digest.store import Store, recipient_key


def test_explicit_smtp_test_needs_no_models_and_preserves_paper_history(config, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    key = recipient_key("reader@example.com")
    with Store(config.state_dir) as store:
        original = store.prepare(key, "real-profile", ["real-paper"], ["real-paper"])
        store.resolve(original, "sent")
    events = []

    class Mailer:
        def __enter__(self):
            events.append("login")
            return self

        def __exit__(self, *_):
            pass

        def send(self, message):
            events.append("send")
            assert "投递联调测试" in message["Subject"]
            assert "真实论文" in message.get_body(preferencelist=("plain",)).get_content()

    def persist():
        with Store(config.state_dir) as store:
            phase = store.db.execute("SELECT status FROM outbox WHERE status!='sent'").fetchone()[0]
        events.append(phase)

    result = send_test(config, outbox_hook=persist, mailer_factory=lambda _: Mailer())
    assert events == ["login", "prepared", "pending", "send"]
    assert result["emails_sent"] == 1
    health = json.loads((Path(result["output_dir"]) / "health.json").read_text("utf-8"))
    assert health["status"] == "success"
    assert health["usage"]["totals"]["attempts"] == 0
    with Store(config.state_dir) as store:
        assert not store.pending()
        assert store.db.execute("SELECT COUNT(*) FROM sent").fetchone()[0] == 1
        assert store.db.execute("SELECT COUNT(*) FROM reviewed").fetchone()[0] == 1


def test_delivery_test_rejects_existing_pending_before_smtp(config):
    with Store(config.state_dir) as store:
        store.prepare(recipient_key("reader@example.com"), "real-profile", [], [])

    def no_mailer(_):
        raise AssertionError("Pending must block even SMTP authentication")

    with pytest.raises(ServiceError, match="pending"):
        send_test(config, mailer_factory=no_mailer)


def test_delivery_test_uncertain_smtp_acceptance_keeps_pending_and_failure_health(config):
    class BrokenMailer:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def send(self, _):
            raise ServiceError("SMTP acceptance uncertain")

    with pytest.raises(ServiceError, match="uncertain"):
        send_test(config, outbox_hook=lambda: None, mailer_factory=lambda _: BrokenMailer())
    with Store(config.state_dir) as store:
        assert len(store.pending()) == 1
        assert store.db.execute("SELECT COUNT(*) FROM sent").fetchone()[0] == 0
    health = json.loads(next(config.output_dir.rglob("health.json")).read_text("utf-8"))
    assert health["status"] == "failed"
