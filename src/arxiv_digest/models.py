from __future__ import annotations

from dataclasses import asdict, dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator


@dataclass(frozen=True)
class Paper:
    id: str
    title: str
    abstract: str
    authors: list[str]
    categories: list[str]
    published: str

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


@dataclass(frozen=True)
class Pick:
    paper: Paper
    decision: Decision
    summary: Summary

    def to_dict(self) -> dict:
        return {
            "paper": self.paper.to_dict(),
            "decision": self.decision.model_dump(),
            "summary": self.summary.model_dump(),
        }
