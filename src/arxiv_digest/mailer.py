from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import format_datetime, formataddr

from .config import Config, email_address, require_env
from .http import ServiceError

LOG = logging.getLogger(__name__)


def persist_safe_cleanup(outbox_hook):
    if outbox_hook:
        try:
            outbox_hook()
        except Exception:
            # The workflow's final state save will try again; preserve the original failure.
            LOG.warning("Safe delivery cleanup could not yet be persisted")


class DeliveryNotAccepted(ServiceError):
    """The SMTP server explicitly rejected this single-recipient message."""


def send_prepared(store, delivery, mailer, message, *, outbox_hook=None):
    """Persist safe preparation, then uncertain intent, before permitting SMTP DATA."""
    try:
        if outbox_hook:
            outbox_hook()
        store.begin_attempt(delivery)
        if outbox_hook:
            outbox_hook()
    except BaseException:
        # send() has never been invoked: this is a provably unsent delivery.
        store.cancel_before_send(delivery)
        persist_safe_cleanup(outbox_hook)
        raise
    try:
        mailer.send(message)
    except DeliveryNotAccepted:
        store.resolve(delivery, "retry")
        persist_safe_cleanup(outbox_hook)
        raise
    # Other exceptions leave pending intact: the server may have accepted DATA.
    store.resolve(delivery, "sent")


def build_message(
    config: Config, recipient: str, delivery_id: str, subject: str, text: str, html: str, now
) -> EmailMessage:
    sender = email_address(require_env("EMAIL_FROM"))
    message = EmailMessage()
    message["From"] = formataddr((config.email.sender_name, sender))
    message["To"] = email_address(recipient)
    message["Subject"] = subject
    message["Date"] = format_datetime(now)
    message["Message-ID"] = f"<{delivery_id}@{sender.split('@')[1]}>"
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


class SMTPMailer:
    def __init__(self, config: Config):
        self.config = config
        self.smtp = None

    def __enter__(self):
        settings = self.config.email
        context = ssl.create_default_context()
        try:
            if settings.security == "ssl":
                self.smtp = smtplib.SMTP_SSL(
                    settings.host, settings.port, timeout=settings.timeout_seconds, context=context
                )
            else:
                self.smtp = smtplib.SMTP(
                    settings.host, settings.port, timeout=settings.timeout_seconds
                )
                self.smtp.ehlo()
                self.smtp.starttls(context=context)
                self.smtp.ehlo()
            self.smtp.login(require_env("SMTP_USERNAME"), require_env("SMTP_PASSWORD"))
        except (smtplib.SMTPException, OSError):
            if self.smtp:
                self.smtp.close()
            raise ServiceError("SMTP: connection, TLS, or authentication failed") from None
        return self

    def send(self, message: EmailMessage):
        try:
            refused = self.smtp.send_message(message)
            if refused:
                raise DeliveryNotAccepted("SMTP: recipient refused")
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
            raise DeliveryNotAccepted("SMTP: message explicitly rejected; safe to retry") from None
        except (smtplib.SMTPException, OSError):
            raise ServiceError(
                "SMTP: delivery failed or acceptance is uncertain; inspect the pending delivery"
            ) from None

    def __exit__(self, *_):
        try:
            self.smtp.quit()
        except (smtplib.SMTPException, OSError):
            # DATA acceptance already determines delivery status; QUIT is best-effort.
            self.smtp.close()
