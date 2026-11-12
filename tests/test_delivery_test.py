from arxiv_digest.operations import test_delivery as send_test
from arxiv_digest.store import Store, recipient_key


def test_legacy_smtp_test_never_sends_or_changes_history(config, monkeypatch):
    for name in ("TYPESAFE_API_KEY", "DEEPSEEK_API_KEY", "SMTP_PASSWORD", "EMAIL_TO"):
        monkeypatch.delenv(name, raising=False)
    key = recipient_key("reader@example.com")
    with Store(config.state_dir) as store:
        original = store.prepare(key, "real-profile", ["real-paper"], ["real-paper"])
        store.resolve(original, "sent")
        pending = store.prepare(key, "real-profile", ["next-paper"], ["next-paper"])

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Disabled test emails must not contact SMTP or persist an outbox")

    result = send_test(config, outbox_hook=unexpected, mailer_factory=unexpected)
    assert result["emails_sent"] == 0
    assert result["skip_reason"] == "non_paper_email_disabled"
    with Store(config.state_dir) as store:
        assert store.db.execute("SELECT COUNT(*) FROM sent").fetchone()[0] == 1
        assert store.db.execute("SELECT COUNT(*) FROM reviewed").fetchone()[0] == 1
        assert store.pending()[0]["id"] == pending
