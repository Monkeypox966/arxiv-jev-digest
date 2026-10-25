import json

import httpx
import pytest
from conftest import jev_response
from pydantic import ValidationError

from arxiv_digest.clients import JevClient, decision_key
from arxiv_digest.config import JevSettings, validate_credentials
from arxiv_digest.http import ServiceError


@pytest.mark.parametrize(
    "backend,key_env",
    [
        ("typesafe", "TYPESAFE_API_KEY"),
        ("openrouter", "OPENROUTER_API_KEY"),
        ("bailian", "DECISION_API_KEY"),
        ("my-provider", "DECISION_API_KEY"),
    ],
)
def test_compatible_backend_uses_its_endpoint_model_and_credentials(
    config, paper, monkeypatch, backend, key_env
):
    config.jev = JevSettings(
        backend=backend,
        endpoint="https://decision.test.invalid/custom-route",
        model="requested-decision-model",
    )
    monkeypatch.setenv(key_env, "fake-provider-key")
    validate_credentials(config, send=False)
    captured = []

    def handler(request):
        captured.append(request)
        data = jev_response(config)
        data["model"] = "resolved-model-version"
        return httpx.Response(200, json=data)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        decision = JevClient(config, client).judge(paper)
    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == config.jev.endpoint
    assert request.headers["Authorization"] == "Bearer fake-provider-key"
    body = json.loads(request.content)
    assert body["model"] == "requested-decision-model"
    assert body["state"]["abstract"] == paper.abstract
    assert set(body["questions"]) == {
        "interest_agents",
        "interest_efficiency",
        "interest_evaluation",
        "exclude_medical_imaging",
    }
    assert decision.model == "resolved-model-version"
    assert decision.score > 0


def test_custom_key_is_shared_by_validation_and_client_without_legacy_fallback(config, monkeypatch):
    config.jev = JevSettings(
        backend="private-provider",
        endpoint="https://decision.test.invalid/v1/systemone",
        model="private-model",
        api_key_env="PRIVATE_DECISION_KEY",
    )
    monkeypatch.delenv("PRIVATE_DECISION_KEY", raising=False)
    with httpx.Client() as client:
        with pytest.raises(ValueError, match="PRIVATE_DECISION_KEY"):
            validate_credentials(config, send=False)
        with pytest.raises(ValueError, match="PRIVATE_DECISION_KEY"):
            JevClient(config, client)
        monkeypatch.setenv("PRIVATE_DECISION_KEY", "fake-private-key")
        validate_credentials(config, send=False)
        assert JevClient(config, client).key == "fake-private-key"


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"model": "custom"},
        {"endpoint": "https://decision.test.invalid/v1/systemone"},
    ],
)
def test_custom_backend_requires_explicit_endpoint_and_model(fields):
    with pytest.raises(ValidationError, match="explicit endpoint and model"):
        JevSettings(backend="custom", **fields)


@pytest.mark.parametrize("name", ["", "KEY=secret", "secret-key", "KEY\nOTHER", "123KEY"])
def test_invalid_key_variable_rejected(name):
    with pytest.raises(ValidationError):
        JevSettings(api_key_env=name)


def test_backend_and_endpoint_separate_cache_but_credentials_do_not(config, paper):
    original = decision_key(config, paper)
    config.jev.api_key_env = "ROTATED_KEY_NAME"
    assert decision_key(config, paper) == original
    config.jev.backend = "other"
    assert decision_key(config, paper) != original
    config.jev.backend = "typesafe"
    config.jev.endpoint = "https://decision.test.invalid/v1/systemone"
    assert decision_key(config, paper) != original


@pytest.mark.parametrize(
    "response", [{"answers": []}, {"result": {"answers": {}}}, {"choices": []}]
)
def test_custom_backend_cannot_silently_accept_a_different_protocol(
    config, paper, monkeypatch, response
):
    config.jev = JevSettings(
        backend="custom",
        endpoint="https://decision.test.invalid/v1/systemone",
        model="custom-decision-model",
    )
    monkeypatch.setenv("DECISION_API_KEY", "fake-decision-key")
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    ) as client:
        with pytest.raises(ServiceError, match="missing answers"):
            JevClient(config, client).judge(paper)
