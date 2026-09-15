"""Thin HTTP client for the apm_connectors `/tools/*` API.

This is the *only* way anything in this repo touches the outside world's
Gmail/Calendar/Excel/Drive/Salesforce/Jira accounts. It never imports
apm_connectors' internals -- same boundary apm_connectors keeps with its
own MCP server (see that repo's CLAUDE.md and docs/api-contract.md).

Every write method here returns whatever apm_connectors returns: a
paused `RunOutcomeResponse` with `pending_action` set and
`final_result: null`. Nothing executes in a real system until a human
approves the returned `action_id` via `decide_action` -- this client has
no way to skip that, on purpose.
"""

from __future__ import annotations

from typing import Any

import httpx

from apm_orchestrator.config import Settings, load_settings


class ConnectorError(RuntimeError):
    """Raised when apm_connectors returns a non-2xx response."""


class ConnectorsClient:
    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """`transport` is a test seam (e.g. `httpx.MockTransport`) -- real
        callers never need it."""
        self._settings = settings or load_settings()
        headers = {}
        if self._settings.connectors_api_key:
            headers["Authorization"] = f"Bearer {self._settings.connectors_api_key}"
        self._http = httpx.AsyncClient(
            base_url=self._settings.connectors_base_url,
            headers=headers,
            timeout=30.0,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "ConnectorsClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        body = {k: v for k, v in body.items() if v is not None}
        try:
            response = await self._http.post(path, json=body)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"apm_connectors call to {path} failed: {exc}") from exc
        if response.status_code >= 400:
            raise ConnectorError(
                f"apm_connectors {path} returned {response.status_code}: {response.text}"
            )
        return response.json()

    async def _get(self, path: str) -> dict[str, Any] | None:
        """Returns `None` on a 404 (apm_connectors treats "unknown
        process_id" as a legitimate answer, not an error -- see that
        repo's scripts/api_smoke_test.py), raises on any other failure."""
        try:
            response = await self._http.get(path)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"apm_connectors call to {path} failed: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ConnectorError(
                f"apm_connectors {path} returned {response.status_code}: {response.text}"
            )
        return response.json()

    # -- Gmail --------------------------------------------------------

    async def gmail_search_emails(
        self, query: str, max_results: int = 10, process_id: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._post(
            "/tools/gmail/search",
            {"process_id": process_id, "query": query, "max_results": max_results},
        )

    async def gmail_read_message(
        self, message_id: str, process_id: str | None = None
    ) -> dict[str, Any]:
        return await self._post(
            "/tools/gmail/read", {"process_id": process_id, "message_id": message_id}
        )

    async def gmail_send_email(
        self, to: str, subject: str, body: str, process_id: str | None = None
    ) -> dict[str, Any]:
        return await self._post(
            "/tools/gmail/send",
            {"process_id": process_id, "to": to, "subject": subject, "body": body},
        )

    # -- Google Calendar ------------------------------------------------

    async def calendar_search_events(
        self,
        query: str | None = None,
        time_min: str | None = None,
        time_max: str | None = None,
        max_results: int = 10,
        process_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._post(
            "/tools/calendar/search",
            {
                "process_id": process_id,
                "query": query,
                "time_min": time_min,
                "time_max": time_max,
                "max_results": max_results,
            },
        )

    async def calendar_read_event(
        self, event_id: str, process_id: str | None = None
    ) -> dict[str, Any]:
        return await self._post(
            "/tools/calendar/read", {"process_id": process_id, "event_id": event_id}
        )

    async def calendar_create_event(
        self,
        title: str,
        start: str,
        end: str,
        attendees: list[str] | None = None,
        location: str | None = None,
        process_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._post(
            "/tools/calendar/create-event",
            {
                "process_id": process_id,
                "title": title,
                "start": start,
                "end": end,
                "attendees": attendees,
                "location": location,
            },
        )

    # -- Excel ----------------------------------------------------------

    async def excel_list_worksheets(self, process_id: str | None = None) -> list[str]:
        return await self._post("/tools/excel/worksheets", {"process_id": process_id})

    async def excel_read_range(
        self,
        sheet_name: str | None = None,
        address: str | None = None,
        process_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._post(
            "/tools/excel/read",
            {"process_id": process_id, "sheet_name": sheet_name, "address": address},
        )

    async def excel_write_range(
        self,
        sheet_name: str,
        address: str,
        values: list[list[Any]],
        process_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._post(
            "/tools/excel/write",
            {
                "process_id": process_id,
                "sheet_name": sheet_name,
                "address": address,
                "values": values,
            },
        )

    # -- Drive documents --------------------------------------------------

    async def drive_list_files(
        self,
        name_contains: str | None = None,
        max_results: int = 20,
        process_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._post(
            "/tools/drive/list",
            {
                "process_id": process_id,
                "name_contains": name_contains,
                "max_results": max_results,
            },
        )

    async def drive_read_file(
        self, file_id: str, process_id: str | None = None
    ) -> dict[str, Any]:
        return await self._post(
            "/tools/drive/read", {"process_id": process_id, "file_id": file_id}
        )

    async def drive_upload_file(
        self,
        name: str,
        content_base64: str,
        mime_type: str,
        process_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._post(
            "/tools/drive/upload",
            {
                "process_id": process_id,
                "name": name,
                "content_base64": content_base64,
                "mime_type": mime_type,
            },
        )

    async def drive_update_file(
        self, file_id: str, content_base64: str, process_id: str | None = None
    ) -> dict[str, Any]:
        return await self._post(
            "/tools/drive/update",
            {
                "process_id": process_id,
                "file_id": file_id,
                "content_base64": content_base64,
            },
        )

    # -- Salesforce -------------------------------------------------------

    async def salesforce_query_records(
        self, soql: str, process_id: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._post(
            "/tools/salesforce/query", {"process_id": process_id, "soql": soql}
        )

    async def salesforce_get_record(
        self, object_name: str, record_id: str, process_id: str | None = None
    ) -> dict[str, Any]:
        return await self._post(
            "/tools/salesforce/read",
            {"process_id": process_id, "object_name": object_name, "record_id": record_id},
        )

    async def salesforce_create_record(
        self, object_name: str, fields: dict[str, Any], process_id: str | None = None
    ) -> dict[str, Any]:
        return await self._post(
            "/tools/salesforce/create",
            {"process_id": process_id, "object_name": object_name, "fields": fields},
        )

    async def salesforce_update_record(
        self,
        object_name: str,
        record_id: str,
        fields: dict[str, Any],
        process_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._post(
            "/tools/salesforce/update",
            {
                "process_id": process_id,
                "object_name": object_name,
                "record_id": record_id,
                "fields": fields,
            },
        )

    # -- Jira ---------------------------------------------------------------

    async def jira_search_issues(
        self, jql: str, max_results: int | None = None, process_id: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._post(
            "/tools/jira/search",
            {"process_id": process_id, "jql": jql, "max_results": max_results},
        )

    async def jira_get_issue(
        self, issue_key: str, process_id: str | None = None
    ) -> dict[str, Any]:
        return await self._post(
            "/tools/jira/read", {"process_id": process_id, "issue_key": issue_key}
        )

    async def jira_create_issue(
        self, fields: dict[str, Any], process_id: str | None = None
    ) -> dict[str, Any]:
        return await self._post(
            "/tools/jira/create", {"process_id": process_id, "fields": fields}
        )

    async def jira_update_issue(
        self, issue_key: str, fields: dict[str, Any], process_id: str | None = None
    ) -> dict[str, Any]:
        return await self._post(
            "/tools/jira/update",
            {"process_id": process_id, "issue_key": issue_key, "fields": fields},
        )

    # -- Approvals ------------------------------------------------------------

    async def decide_action(self, action_id: str, approved: bool) -> dict[str, Any]:
        return await self._post(
            f"/tools/actions/{action_id}/decision", {"approved": approved}
        )

    async def get_action_status(self, action_id: str) -> dict[str, Any] | None:
        """`None` while `action_id` is still awaiting a human decision --
        apm_connectors' state store only sets a process's status once its
        graph reaches `execute_node` (i.e. only after approval or
        rejection; see that repo's graph.py), so "no status yet" is what
        "still pending" looks like over this route. Used by the poller
        to find out whether a case's `wait_for_approval` can resume."""
        return await self._get(f"/processes/{action_id}/status")
