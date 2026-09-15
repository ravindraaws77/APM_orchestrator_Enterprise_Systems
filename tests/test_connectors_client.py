import httpx
import pytest

from apm_orchestrator.config import Settings
from apm_orchestrator.connectors_client import ConnectorError, ConnectorsClient


def _client_with_transport(transport: httpx.MockTransport) -> ConnectorsClient:
    settings = Settings(
        connectors_base_url="http://testserver",
        connectors_api_key="test-key",
        anthropic_api_key=None,
        order_renewal_policy_path=None,
    )
    return ConnectorsClient(settings=settings, transport=transport)


@pytest.mark.asyncio
async def test_gmail_search_emails_sends_auth_header_and_drops_none_fields():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["headers"] = request.headers
        seen["body"] = request.read()
        return httpx.Response(200, json=[{"message_id": "m1"}])

    client = _client_with_transport(httpx.MockTransport(handler))
    result = await client.gmail_search_emails(query="newer_than:14d renewal")

    assert result == [{"message_id": "m1"}]
    assert seen["path"] == "/tools/gmail/search"
    assert seen["headers"]["authorization"] == "Bearer test-key"
    import json

    body = json.loads(seen["body"])
    assert body == {"query": "newer_than:14d renewal", "max_results": 10}
    assert "process_id" not in body

    await client.aclose()


@pytest.mark.asyncio
async def test_write_route_returns_pending_action():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "action_id": "abc-123",
                "pending_action": {"type": "approval_request", "tool": "gmail"},
                "final_result": None,
            },
        )

    client = _client_with_transport(httpx.MockTransport(handler))
    result = await client.gmail_send_email(
        to="customer@realcorp.io", subject="Renewal", body="..."
    )

    assert result["action_id"] == "abc-123"
    assert result["final_result"] is None
    assert result["pending_action"]["tool"] == "gmail"

    await client.aclose()


@pytest.mark.asyncio
async def test_non_2xx_raises_connector_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="excel not configured")

    client = _client_with_transport(httpx.MockTransport(handler))

    with pytest.raises(ConnectorError):
        await client.excel_list_worksheets()

    await client.aclose()
