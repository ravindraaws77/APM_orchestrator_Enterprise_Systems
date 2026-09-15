"""Regression test for a real bug: a write route's response has two
different values both called `action_id` -- the top-level one
apm_connectors' /status and /decision routes actually key on, and an
unrelated internal state-store id nested inside `pending_action` that
happens to share the field name. Live-verified more than once: the SDK
agent read the wrong one when reporting a pending action's id back to a
human reviewer. `_ok` now strips the inner one so a tool result only
ever has one field named `action_id`.
"""

from __future__ import annotations

import json

from apm_orchestrator.tools import _ok


def test_ok_strips_internal_pending_action_id():
    raw = {
        "action_id": "real-process-id",
        "summary": None,
        "pending_action": {
            "type": "approval_request",
            "action_id": "internal-bookkeeping-id",
            "tool": "jira",
            "method": "create_issue",
            "description": "Create Jira issue",
            "payload": {},
        },
        "final_result": None,
    }

    result = _ok(raw)
    data = json.loads(result["content"][0]["text"])

    assert data["action_id"] == "real-process-id"
    assert "action_id" not in data["pending_action"]
    assert data["pending_action"]["tool"] == "jira"  # other fields untouched


def test_ok_passes_through_read_results_unchanged():
    raw = [{"record_id": "opp1", "fields": {"Name": "x"}}]

    result = _ok(raw)
    data = json.loads(result["content"][0]["text"])

    assert data == raw


def test_ok_passes_through_result_with_no_pending_action():
    raw = {"action_id": "abc", "final_result": {"executed": True}, "pending_action": None}

    result = _ok(raw)
    data = json.loads(result["content"][0]["text"])

    assert data == raw
