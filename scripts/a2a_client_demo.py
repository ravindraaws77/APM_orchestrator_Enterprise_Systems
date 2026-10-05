"""Talk to the APM Supervisor the way another agent would, over A2A.

Fetches the Agent Card, prints the skills it advertises, then sends one
business request with `SendMessage` and prints the resulting task.
Plain httpx + JSON-RPC on purpose, so the protocol is visible end to
end; a real caller would typically use the a2a-sdk client.

    python -m apm_orchestrator.a2a_server            # terminal 1
    python scripts/a2a_client_demo.py "Acme wants to renew their licence"

Needs A2A_API_KEY to match the server's if the server sets one. The
request runs a real Supervisor turn (real Claude API spend), and any
write it leads to is only a *proposal* waiting for human approval in
apm_connectors.
"""

from __future__ import annotations

import os
import sys
import uuid

import httpx

BASE_URL = os.environ.get("A2A_PUBLIC_URL", "http://127.0.0.1:9999")


def main() -> None:
    request_text = " ".join(sys.argv[1:]) or "Acme Ltd wants to renew their annual licence before it lapses next month."
    headers = {"A2A-Version": "1.0"}
    if os.environ.get("A2A_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['A2A_API_KEY']}"

    with httpx.Client(timeout=300) as http:
        card = http.get(f"{BASE_URL}/.well-known/agent-card.json").raise_for_status().json()
        print(f"Agent: {card['name']} v{card['version']}")
        for skill in card["skills"]:
            print(f"  skill {skill['id']}: {skill['name']}")
        rpc_url = card["supportedInterfaces"][0]["url"]

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": str(uuid.uuid4()),
                    "role": "ROLE_USER",
                    "parts": [{"text": request_text}],
                }
            },
        }
        body = http.post(rpc_url, json=payload, headers=headers).raise_for_status().json()

    if "error" in body:
        sys.exit(f"A2A error: {body['error']}")
    task = body["result"]["task"]
    print(f"\nTask {task['id']}: {task['status']['state']}")
    for artifact in task.get("artifacts", []):
        for part in artifact["parts"]:
            print(part.get("text", ""))


if __name__ == "__main__":
    main()
