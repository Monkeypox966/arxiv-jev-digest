from __future__ import annotations

import copy
from pathlib import Path

import pytest

from arxiv_digest.config import ArxivSettings, Config, Interest
from arxiv_digest.models import Paper, Summary

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config(tmp_path, monkeypatch):
    # Keep behavior tests independent of the user's editable subscription profile.
    cfg = Config(
        arxiv=ArxivSettings(categories=["cs.AI", "cs.CL", "cs.LG", "cs.CV"]),
        interests=[
            Interest(
                id="agents", label="Agents", text="Proposes methods for language model agents."
            ),
            Interest(
                id="efficiency",
                label="Efficiency",
                text="Reduces language model inference costs.",
                weight=0.9,
            ),
            Interest(
                id="evaluation",
                label="Evaluation",
                text="Introduces language model evaluation methods.",
                weight=0.9,
            ),
        ],
        exclusions=[
            Interest(
                id="medical_imaging", label="Medical", text="The primary topic is medical imaging."
            )
        ],
    )
    cfg.state_dir = tmp_path / "state"
    cfg.output_dir = tmp_path / "output"
    cfg.jev.concurrency = 1
    cfg.deepseek.concurrency = 1
    cfg.email.host = "smtp.test.invalid"
    for key, value in {
        "TYPESAFE_API_KEY": "fake-jev",
        "OPENROUTER_API_KEY": "fake-openrouter",
        "DEEPSEEK_API_KEY": "fake-deepseek",
        "SMTP_USERNAME": "sender@example.com",
        "SMTP_PASSWORD": "fake-smtp",
        "EMAIL_FROM": "sender@example.com",
        "EMAIL_TO": "reader@example.com",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("arxiv_digest.http.RateLimiter.wait", lambda _: None)
    monkeypatch.setattr("arxiv_digest.http.time.sleep", lambda _: None)
    return copy.deepcopy(cfg)


@pytest.fixture
def paper():
    return Paper(
        id="2610.00001",
        title="A reliable language agent",
        abstract="An abstract about agents.",
        authors=["A. Author"],
        categories=["cs.AI", "cs.CL"],
        published="2026-10-23T00:00:00Z",
    )


@pytest.fixture
def summary():
    return Summary(
        title_zh="可靠的语言智能体",
        tldr="提高工具使用的可靠性。",
        problem="工具结果可能不可靠。",
        method="检查工具返回内容。",
        results="摘要未报告具体实验数值。",
        limitations="仅依据摘要，尚需阅读全文。",
    )


def atom(ids, total=None, *, abstract="An abstract about language agents."):
    entries = "".join(
        f"""<entry><id>http://arxiv.org/abs/{id}</id><title>Agent paper {id}</title>
        <summary>{abstract}</summary><published>2026-10-23T00:00:00Z</published>
        <author><name>Researcher</name></author><category term="cs.AI"/>
        </entry>"""
        for id in ids
    )
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
        f"<opensearch:totalResults>{len(ids) if total is None else total}"
        f"</opensearch:totalResults>{entries}</feed>"
    )


def jev_response(config, interest=0.95, exclusion=0.01):
    return {
        "model": "jev-1.14.0",
        "answers": {
            **{f"interest_{i.id}": {"type": "noul", "noul": interest} for i in config.interests},
            **{f"exclude_{e.id}": {"type": "noul", "noul": exclusion} for e in config.exclusions},
        },
        "usage": {"input_tokens": 600, "output_tokens": 20},
    }
