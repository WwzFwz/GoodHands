import json

import httpx
import pytest

from goodhands.models import Profile, ProviderSettings
from goodhands.provider import ChatProvider, ProviderError


def test_openrouter_adapter_sends_supported_tool_contract(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "only-for-test")

    def respond(request):
        assert request.url == "https://openrouter.ai/api/v1/chat/completions"
        body = json.loads(request.content)
        assert body["provider"]["require_parameters"] is True
        assert body["tools"][0]["function"]["name"] == "read_file"
        assert body["stream"] is False
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "tool_calls": []}}],
                "usage": {"cost": 0.001},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        data = ChatProvider(ProviderSettings(), client).complete(
            profile=Profile(model="example/model"),
            messages=[],
            tools=[{"type": "function", "function": {"name": "read_file"}}],
            stage="coder",
        )
    assert data["usage"]["cost"] == 0.001


@pytest.mark.parametrize("code", [401, 402, 429, 500])
def test_provider_failure_does_not_leak_key_or_body(monkeypatch, code):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sensitive-key")
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(code, text="sensitive-key"))
    ) as client:
        with pytest.raises(ProviderError) as caught:
            ChatProvider(ProviderSettings(), client).complete(
                profile=Profile(model="test"), messages=[], tools=[], stage="coder"
            )
    assert "sensitive-key" not in str(caught.value)
    assert str(code) in str(caught.value)


def test_compatible_provider_does_not_send_openrouter_fields(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test")

    def respond(request):
        assert "provider" not in json.loads(request.content)
        return httpx.Response(200, json={"choices": []})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        settings = ProviderSettings(
            kind="compatible",
            base_url="https://ai-gateway.vercel.sh/v1",
            api_key_env="AI_GATEWAY_API_KEY",
        )
        ChatProvider(settings, client).complete(
            profile=Profile(model="test"), messages=[], tools=[], stage="coder"
        )


def test_missing_key_fails_without_network(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        ChatProvider(ProviderSettings()).preflight()


def test_whole_workflow_through_http_adapter(store, settings, task, monkeypatch):
    from goodhands.demo import DemoProvider
    from goodhands.engine import Engine

    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-test-key")
    settings.profiles["default"].model = "test/model"
    simulator = DemoProvider()

    def respond(request):
        body = json.loads(request.content)
        stage = json.loads(body["messages"][1]["content"])["stage"]
        response = simulator.complete(
            profile=settings.profiles["default"],
            messages=body["messages"],
            tools=body["tools"],
            stage=stage,
        )
        response["usage"]["cost"] = 0.001
        return httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        engine = Engine(store, ChatProvider(settings.provider, client), allow_local=True)
        state = engine.execute(engine.create(task, settings)["id"])
    assert state["status"] == "completed"
    assert state["charged_usd"] == pytest.approx(state["model_calls"] * 0.001)
    assert state["cost_accounting"]["reported"] == state["model_calls"]
