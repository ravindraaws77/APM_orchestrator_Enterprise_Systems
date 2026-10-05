"""A2A server: Agent Card, auth, and the SendMessage round trip.

Uses a fake Supervisor, so no Claude API call and no apm_connectors
server -- this checks the A2A wiring, not routing quality (that's
test_supervisor_routing_eval.py's job).
"""

from __future__ import annotations

import pytest

pytest.importorskip("a2a", reason="install the 'a2a' extra to run A2A tests")

from starlette.testclient import TestClient  # noqa: E402

from apm_orchestrator.a2a_server import RPC_PATH, build_app  # noqa: E402

CARD_PATH = "/.well-known/agent-card.json"
# A2A clients send the protocol version; without it the SDK assumes 0.3.
V1 = {"A2A-Version": "1.0"}


def _send(text: str, message_id: str = "m1") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {"message": {"messageId": message_id, "role": "ROLE_USER", "parts": [{"text": text}]}},
    }


def _client(api_key: str | None = None, calls: list[str] | None = None) -> TestClient:
    async def fake_supervisor(prompt: str) -> str:
        if calls is not None:
            calls.append(prompt)
        return f"Routed to Order-Renewal; renewal email proposed for approval. ({prompt})"

    return TestClient(build_app(base_url="https://apm.example", supervisor=fake_supervisor, api_key=api_key))


def test_agent_card_advertises_both_business_skills_and_rpc_url():
    card = _client().get(CARD_PATH).json()
    assert card["name"] == "APM Supervisor"
    assert {s["id"] for s in card["skills"]} == {"order_renewal", "customer_onboarding"}
    assert card["supportedInterfaces"][0]["url"] == "https://apm.example" + RPC_PATH
    assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"


def test_card_states_that_writes_need_human_approval():
    card = _client().get(CARD_PATH).json()
    assert "human" in card["description"].lower()


def test_send_message_returns_completed_task_with_supervisor_text():
    calls: list[str] = []
    res = _client(calls=calls).post(RPC_PATH, json=_send("Acme wants to renew their licence"), headers=V1)
    task = res.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert "renewal email proposed" in task["artifacts"][0]["parts"][0]["text"]
    assert calls == ["Acme wants to renew their licence"]


def test_blank_request_is_rejected_without_calling_the_supervisor():
    calls: list[str] = []
    res = _client(calls=calls).post(RPC_PATH, json=_send("   "), headers=V1)
    body = res.json()
    # Either the SDK refuses the empty part outright, or the executor rejects the task.
    if "result" in body:
        assert body["result"]["task"]["status"]["state"] == "TASK_STATE_REJECTED"
    else:
        assert "error" in body
    assert calls == []


def test_rpc_requires_bearer_token_when_api_key_set_but_card_stays_public():
    client = _client(api_key="s3cret")
    assert client.get(CARD_PATH).status_code == 200
    assert client.post(RPC_PATH, json=_send("renew Acme"), headers=V1).status_code == 401
    assert client.post(RPC_PATH, json=_send("renew Acme"), headers={**V1, "Authorization": "Bearer wrong"}).status_code == 401
    ok = client.post(RPC_PATH, json=_send("renew Acme"), headers={**V1, "Authorization": "Bearer s3cret"})
    assert ok.json()["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"


def test_card_declares_bearer_scheme_only_when_auth_is_on():
    assert "securitySchemes" not in _client().get(CARD_PATH).json()
    card = _client(api_key="k").get(CARD_PATH).json()
    assert card["securitySchemes"]["bearer"]["httpAuthSecurityScheme"]["scheme"] == "Bearer"


def test_a2a_layer_never_exposes_the_approval_decision():
    """CLAUDE.md's approval boundary: no A2A path may approve a pending action."""
    import inspect

    import apm_orchestrator.a2a_server as mod

    source = inspect.getsource(mod)
    assert "decide_action(" not in source
    assert "/decision" not in source
