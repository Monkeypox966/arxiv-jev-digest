from __future__ import annotations

import re
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
    version: str = ""
    updated: str = ""
    announcement_type: Literal["new", "cross", "replace"] = "new"

    @property
    def tracking_id(self) -> str:
        if self.announcement_type != "replace":
            return self.id
        if not self.version:
            raise ValueError("Replacement announcements require an explicit version")
        return self.id + "@" + self.versioned_id.removeprefix(self.id)

    @property
    def versioned_id(self) -> str:
        if not self.version:
            return self.id
        match = re.fullmatch(r"v?([1-9]\d*)", self.version)
        if not match:
            raise ValueError("Invalid arXiv paper version")
        return f"{self.id}v{match[1]}"

    @property
    def url(self) -> str:
        return f"https://arxiv.org/abs/{self.id}"

    @property
    def pdf_url(self) -> str:
        return f"https://arxiv.org/pdf/{self.id}"

    @property
    def metadata_url(self) -> str:
        return f"https://arxiv.org/abs/{self.versioned_id}"

    @property
    def metadata_pdf_url(self) -> str:
        return f"https://arxiv.org/pdf/{self.versioned_id}"

    @property
    def metadata_html_url(self) -> str:
        return f"https://arxiv.org/html/{self.versioned_id}"

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
    tldr: str = Field(min_length=1)  # Complete abstract translation; no editorial length cap.
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
    authors: list[str] = Field(default_factory=list)  # Legacy data only; not required or scored.
    source: str | None = None

    @field_validator("institution", mode="before")
    @classmethod
    def strip_institution(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("source")
    @classmethod
    def safe_source(cls, value):
        return PaperDetails.safe_project_url(value)


class PaperDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_url: str | None = None
    project_source: str | None = None
    project_label: str = "项目网站"
    project_status: Literal["unknown", "found", "not_found", "unavailable"] = "unknown"
    affiliation_status: Literal["unknown", "found", "partial", "not_found", "unavailable"] = (
        "unknown"
    )
    affiliations: list[Affiliation] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    reviewed_on: str = ""
    reviewed_version: str = ""
    failed_sources: list[str] = Field(default_factory=list)
    source_diagnostics: dict[str, list[str]] = Field(default_factory=dict)

    def completeness(self, paper: Paper) -> dict:
        institutions = {
            " ".join(affiliation.institution.casefold().split())
            for affiliation in self.verified_affiliations()
        }
        project_found = bool(self.project_url and (self.project_source or self.sources))
        return {
            "project_found": project_found,
            "project_status": self.project_status,
            "affiliation_status": self.affiliation_status,
            "institutions_found": bool(institutions),
            "institution_count": len(institutions),
        }

    def verified_affiliations(self) -> list[Affiliation]:
        return [
            affiliation
            for affiliation in self.affiliations
            if affiliation.institution.strip() and (affiliation.source or self.sources)
        ]

    def needs_refresh(self, paper: Paper) -> bool:
        completeness = self.completeness(paper)
        return (
            not completeness["project_found"]
            or not completeness["institutions_found"]
            or self.affiliation_status == "partial"
        )

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

    @field_validator("sources", "failed_sources")
    @classmethod
    def safe_sources(cls, values):
        return [cls.safe_project_url(value) for value in values]


class InstitutionMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    institution: str
    rule: str
    alias: str
    score: float = Field(ge=0, le=1)


class InstitutionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    quote: str = Field(min_length=20, max_length=600)

    @field_validator("source")
    @classmethod
    def safe_source(cls, value):
        return PaperDetails.safe_project_url(value)


class InstitutionAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    institution: str
    canonical_name: str = ""
    score: float = Field(ge=0, le=1)
    status: Literal["assessed", "neutral", "rule"] = "neutral"
    confidence: float = Field(default=0, ge=0, le=1)
    reason: str = Field(default="", max_length=1600)
    sources: list[str] = Field(default_factory=list)
    identity_sources: list[str] = Field(default_factory=list)
    identity_evidence: list[InstitutionEvidence] = Field(default_factory=list, max_length=4)
    evidence: list[InstitutionEvidence] = Field(default_factory=list, max_length=8)
    assessed_on: str = ""
    deferred: bool = False


class InstitutionIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    canonical_name: str = Field(default="", max_length=200)
    website: str = ""
    confidence: float = Field(ge=0, le=1)
    ambiguous: bool = False
    evidence: list[InstitutionEvidence] = Field(default_factory=list, max_length=4)

    @field_validator("website")
    @classmethod
    def safe_website(cls, value):
        return PaperDetails.safe_project_url(value) if value else ""


class Ranking(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    relevance_score: float = Field(ge=0, le=1)
    institution_score: float = Field(ge=0, le=1)
    institution_weight: float = Field(ge=0, le=0.5)
    score: float = Field(ge=0, le=1)
    status: Literal["matched", "assessed", "unlisted", "unknown"]
    matches: list[InstitutionMatch] = Field(default_factory=list)
    assessments: list[InstitutionAssessment] = Field(default_factory=list)


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
