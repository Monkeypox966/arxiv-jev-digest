import smtplib
from datetime import UTC, datetime

import pytest

from arxiv_digest.http import ServiceError
from arxiv_digest.mailer import SMTPMailer, build_message


@pytest.mark.parametrize("security", ["ssl", "starttls"])
def test_smtp_uses_tls_and_authenticated_single_recipient(config, monkeypatch, security):
    events = []

    class SMTP:
        def __init__(self, *args, **kwargs):
            events.append(("connect", args, kwargs))

        def ehlo(self):
            events.append(("ehlo",))

        def starttls(self, **kwargs):
            assert kwargs["context"].check_hostname
            events.append(("starttls",))

        def login(self, username, password):
            assert username == "sender@example.com"
            assert password == "fake-smtp"
            events.append(("login",))

        def send_message(self, message):
            assert message.get_all("To") == ["reader@example.com"]
            events.append(("send",))
            return {}

        def quit(self):
            events.append(("quit",))
            raise smtplib.SMTPServerDisconnected("already closed")

        def close(self):
            events.append(("close",))

    monkeypatch.setattr("arxiv_digest.mailer.smtplib.SMTP_SSL", SMTP)
    monkeypatch.setattr("arxiv_digest.mailer.smtplib.SMTP", SMTP)
    config.email.security = security
    message = build_message(
        config,
        "reader@example.com",
        "delivery-id",
        "中文论文日报",
        "纯文本",
        "<p>摘要</p>",
        datetime.now(UTC),
    )
    with SMTPMailer(config) as mailer:
        mailer.send(message)
    assert message.get_body(preferencelist=("plain",)).get_content().strip() == "纯文本"
    assert message["Message-ID"] == "<delivery-id@example.com>"
    names = [e[0] for e in events]
    assert names.index("login") < names.index("send")
    assert ("starttls" in names) == (security == "starttls")
    assert names[-1] == "close"


def test_smtp_errors_do_not_expose_provider_body(config):
    class SMTP:
        def send_message(self, _):
            raise smtplib.SMTPDataError(550, b"private-provider-error-and-address")

    mailer = SMTPMailer(config)
    mailer.smtp = SMTP()
    with pytest.raises(ServiceError) as exc:
        mailer.send(None)
    assert "private-provider" not in str(exc.value)
