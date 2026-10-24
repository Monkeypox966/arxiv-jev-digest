from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Interest(Settings):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    label: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=10, max_length=1500)
    weight: float = Field(default=1, gt=0, le=1)


class ArxivSettings(Settings):
    categories: list[str] = Field(min_length=1)
    source: Literal["api", "rss"] = "api"
    lookback_days: int = Field(default=7, ge=1, le=31)
    page_size: int = Field(default=200, ge=1, le=2000)
    max_candidates: int = Field(default=10000, ge=1, le=30000)
    request_interval_seconds: float = Field(default=3.1, ge=3)
    include_cross_lists: bool = True

    @field_validator("categories")
    @classmethod
    def validate_categories(cls, values):
        if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9.-]*", v) for v in values):
            raise ValueError("Use arXiv categories such as cs.AI, stat.ML, or math")
        return list(dict.fromkeys(values))


class APISettings(Settings):
    concurrency: int = Field(default=3, ge=1, le=32)
    requests_per_minute: int = Field(default=60, ge=1, le=1200)
    timeout_seconds: float = Field(default=90, gt=0, le=300)
    attempts: int = Field(default=3, ge=1, le=5)


def https_url(value: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("API URLs must use HTTPS without credentials, query, or fragment")
    return value.rstrip("/")


class JevSettings(APISettings):
    backend: Literal["typesafe", "openrouter"] = "typesafe"
    model: str = Field(default="jev-1.14.0", min_length=1)
    endpoint: str = "https://api.typesafe.ai/v1/systemone"
    _https = field_validator("endpoint")(https_url)

    @model_validator(mode="after")
    def check_backend(self):
        if self.backend == "openrouter" and "api.typesafe.ai" in self.endpoint:
            raise ValueError("OpenRouter requires its decisions endpoint and a typesafe/jev model")
        return self


class DeepSeekSettings(APISettings):
    base_url: str = "https://api.deepseek.com"
    model: str = Field(default="deepseek-flash", min_length=1)
    language: str = Field(default="简体中文", min_length=1, max_length=100)
    max_tokens: int = Field(default=1800, ge=500, le=16000)
    disable_thinking: bool = True
    _https = field_validator("base_url")(https_url)


class SelectionSettings(Settings):
    top_k: int = Field(default=30, ge=1, le=100)
    min_score: float = Field(default=0.6, ge=0, le=1)
    exclude_threshold: float = Field(default=0.85, ge=0, le=1)
    combine: Literal["max", "noisy_or"] = "max"


def email_address(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", value):
        raise ValueError("Expected a plain email address")
    return value


class EmailSettings(Settings):
    host: str = Field(default="smtp.example.com", min_length=1)
    port: int = Field(default=465, ge=1, le=65535)
    security: Literal["ssl", "starttls"] = "ssl"
    sender_name: str = "arXiv Paper Digest"
    subject_prefix: str = "arXiv 论文日报"
    recipients: list[str] = Field(default_factory=list)
    send_empty: bool = False
    timeout_seconds: float = Field(default=45, gt=0, le=120)

    @field_validator("host", "sender_name", "subject_prefix")
    @classmethod
    def no_newlines(cls, value):
        if "\r" in value or "\n" in value:
            raise ValueError("Email settings cannot contain newlines")
        return value

    @field_validator("recipients")
    @classmethod
    def validate_recipients(cls, values):
        return list(dict.fromkeys(email_address(v) for v in values))


class RetentionSettings(Settings):
    cache_days: int = Field(default=30, ge=7, le=365)


class Config(Settings):
    timezone: str = "Asia/Shanghai"
    state_dir: Path = Path(".state")
    output_dir: Path = Path("output")
    arxiv: ArxivSettings
    jev: JevSettings = Field(default_factory=JevSettings)
    selection: SelectionSettings = Field(default_factory=SelectionSettings)
    interests: list[Interest] = Field(min_length=1, max_length=30)
    exclusions: list[Interest] = Field(default_factory=list, max_length=30)
    deepseek: DeepSeekSettings = Field(default_factory=DeepSeekSettings)
    email: EmailSettings = Field(default_factory=EmailSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Unknown IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def consistent_settings(self):
        ids = [i.id for i in self.interests + self.exclusions]
        if len(set(ids)) != len(ids):
            raise ValueError("Interest and exclusion IDs must be unique")
        if self.retention.cache_days < self.arxiv.lookback_days:
            raise ValueError("cache_days must cover lookback_days")
        return self


def load_config(path: Path) -> Config:
    config = Config.model_validate(tomllib.loads(path.read_text(encoding="utf-8-sig")))
    config.state_dir = (path.parent / config.state_dir).resolve()
    config.output_dir = (path.parent / config.output_dir).resolve()
    if config.state_dir == config.output_dir or config.state_dir in config.output_dir.parents:
        raise ValueError("output_dir must be outside state_dir")
    return config


def recipients(config: Config) -> list[str]:
    override = os.getenv("EMAIL_TO", "").strip()
    values = re.split(r"[,;]", override) if override else config.email.recipients
    return list(dict.fromkeys(email_address(v) for v in values if v.strip()))


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing {name}; set it in .env or GitHub Actions Secrets")
    return value


def validate_credentials(config: Config, *, send: bool) -> None:
    key = "TYPESAFE_API_KEY" if config.jev.backend == "typesafe" else "OPENROUTER_API_KEY"
    require_env(key)
    require_env("DEEPSEEK_API_KEY")
    if send:
        require_env("SMTP_USERNAME")
        require_env("SMTP_PASSWORD")
        email_address(require_env("EMAIL_FROM"))
        if not recipients(config):
            raise ValueError("Set EMAIL_TO or [email].recipients before sending")
        if config.email.host == "smtp.example.com":
            raise ValueError("Configure [email].host for your SMTP provider")
