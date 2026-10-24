from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


@dataclass(frozen=True)
class Paper:
    id: str
    title: str
    abstract: str
    authors: list[str]
    categories: list[str]
    published: str
    announced: str = ""

    @property
    def url(self) -> str:
        return f"https://arxiv.org/abs/{self.id}"

    @property
    def pdf_url(self) -> str:
        return f"https://arxiv.org/pdf/{self.id}"

    def to_dict(self) -> dict:
        return asdict(self)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    score: float = Field(ge=0, le=1)
    interests: dict[str, float]
    exclusions: dict[str, float]
    excluded: bool
    model: str
    input_tokens: int = Field(default=0, ge=0)


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title_zh: str = Field(min_length=1, max_length=500)
    tldr: str = Field(min_length=1, max_length=1200)
    problem: str = Field(min_length=1, max_length=1800)
    method: str = Field(min_length=1, max_length=1800)
    results: str = Field(min_length=1, max_length=1800)
    limitations: str = Field(min_length=1, max_length=1800)

    @field_validator("*", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class Affiliation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    institution: str = Field(min_length=1, max_length=400)
    authors: list[str] = Field(default_factory=list)
    source: str | None = None

    @field_validator("source")
    @classmethod
    def safe_source(cls, value):
        return PaperDetails.safe_project_url(value)


class PaperDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_url: str | None = None
    project_source: str | None = None
    project_label: str = "项目网站"
    affiliations: list[Affiliation] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    reviewed_on: str = ""

    @field_validator("project_url", "project_source")
    @classmethod
    def safe_project_url(cls, value):
        from urllib.parse import urlsplit

        if value is None:
            return value
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
        ):
            raise ValueError("Expected an HTTP(S) URL without credentials")
        return value

    @field_validator("sources")
    @classmethod
    def safe_sources(cls, values):
        return [cls.safe_project_url(value) for value in values]


class InstitutionMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    institution: str
    rule: str
    alias: str
    score: float = Field(ge=0, le=1)


class Ranking(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    relevance_score: float = Field(ge=0, le=1)
    institution_score: float = Field(ge=0, le=1)
    institution_weight: float = Field(ge=0, le=0.5)
    score: float = Field(ge=0, le=1)
    status: Literal["matched", "unlisted", "unknown"]
    matches: list[InstitutionMatch] = Field(default_factory=list)


@dataclass(frozen=True)
class Pick:
    paper: Paper
    decision: Decision
    summary: Summary
    details: PaperDetails | None = None
    ranking: Ranking | None = None

    def to_dict(self) -> dict:
        return {
            "paper": self.paper.to_dict(),
            "decision": self.decision.model_dump(),
            "summary": self.summary.model_dump(),
            "details": self.details.model_dump() if self.details else None,
            "ranking": self.ranking.model_dump() if self.ranking else None,
        }
