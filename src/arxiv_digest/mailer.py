from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import format_datetime, formataddr

from .config import Config, email_address, require_env
from .http import ServiceError


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
                raise ServiceError("SMTP: recipient refused")
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
