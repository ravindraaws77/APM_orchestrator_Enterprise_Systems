"""Claude Agent SDK tools wrapping `ConnectorsClient`.

One `@tool` per apm_connectors `/tools/*` route. Every business agent
(Order-Renewal today, others later) gets handed a *subset* of these via
its own `allowed_tools` list -- that subset is its toolbelt, scoped to
exactly what its business process needs (see docs/roadmap.md in
apm_connectors: "Agents are decomposed by business process, not by
connector").

Deliberately absent: a tool wrapping `POST /tools/actions/{id}/decision`.
No agent may approve its own proposed write -- the approval boundary is
a real authenticated human hitting that endpoint directly (or via a
human-facing review surface), never something an agent calls itself.
`ConnectorsClient.decide_action` exists for that human-facing surface to
use, not for any agent's toolbelt.

Every tool here is safe to auto-approve at the SDK layer, which is what
listing a tool's fully-qualified name in `ClaudeAgentOptions.allowed_tools`
already does (live-verified: the SDK auto-approves an allowed tool before
any `can_use_tool` callback is even consulted, so no extra permission
plumbing is needed here) -- a "write" tool never performs the real-world
write, it only calls a `/tools/*` route that returns a *proposed*, paused
action. The actual gate is apm_connectors' own approval endpoint, one
layer below anything this SDK controls.
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from apm_orchestrator.connectors_client import ConnectorError, ConnectorsClient

_client: ConnectorsClient | None = None


def get_client() -> ConnectorsClient:
    global _client
    if _client is None:
        _client = ConnectorsClient()
    return _client


async def aclose_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _disambiguate_pending_action(data: Any) -> Any:
    """A write route's response has two different values both called
    "action_id": the top-level one (what apm_connectors' /status and
    /decision routes actually key on) and an unrelated internal
    state-store bookkeeping id nested inside `pending_action`, which
    happens to share that field name. Live-verified more than once: the
    model reads the wrong one when reporting a pending action's id back
    to a human reviewer. Strip the inner one so a tool result only ever
    has one field named `action_id` to read.
    """
    if isinstance(data, dict):
        pending = data.get("pending_action")
        if isinstance(pending, dict) and "action_id" in pending:
            pending = {k: v for k, v in pending.items() if k != "action_id"}
            data = {**data, "pending_action": pending}
    return data


def _ok(data: Any) -> dict[str, Any]:
    data = _disambiguate_pending_action(data)
    return {"content": [{"type": "text", "text": json.dumps(data, default=str)}]}


def _err(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_STR_LIST = {"type": "array", "items": {"type": "string"}}
_FIELDS_OBJ = {"type": "object", "description": "Arbitrary key/value fields payload"}
_VALUES_GRID = {
    "type": "array",
    "items": {"type": "array"},
    "description": "2D grid of cell values, e.g. [[1, 2], [3, 4]]",
}


# -- Gmail --------------------------------------------------------------


@tool(
    "gmail_search_emails",
    "Search Gmail messages by query (Gmail search syntax, e.g. "
    "'from:customer@example.com newer_than:14d'). Read-only.",
    {
        "type": "object",
        "properties": {"query": _STR, "max_results": _INT},
        "required": ["query"],
    },
)
async def gmail_search_emails(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().gmail_search_emails(
            query=args["query"], max_results=args.get("max_results", 10)
        )
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


@tool(
    "gmail_read_message",
    "Read one Gmail message's content and metadata by id. Read-only.",
    {"type": "object", "properties": {"message_id": _STR}, "required": ["message_id"]},
)
async def gmail_read_message(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().gmail_read_message(message_id=args["message_id"])
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


@tool(
    "gmail_send_email",
    "Propose sending an email. This does NOT send anything: apm_connectors "
    "records it as a pending action requiring a separate human approval "
    "before it actually goes out.",
    {
        "type": "object",
        "properties": {"to": _STR, "subject": _STR, "body": _STR},
        "required": ["to", "subject", "body"],
    },
)
async def gmail_send_email(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().gmail_send_email(
            to=args["to"], subject=args["subject"], body=args["body"]
        )
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


# -- Google Calendar ------------------------------------------------------


@tool(
    "calendar_search_events",
    "Search calendar events by query and/or time range. Read-only.",
    {
        "type": "object",
        "properties": {
            "query": _STR,
            "time_min": _STR,
            "time_max": _STR,
            "max_results": _INT,
        },
    },
)
async def calendar_search_events(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().calendar_search_events(
            query=args.get("query"),
            time_min=args.get("time_min"),
            time_max=args.get("time_max"),
            max_results=args.get("max_results", 10),
        )
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


@tool(
    "calendar_read_event",
    "Read one calendar event's details by id. Read-only.",
    {"type": "object", "properties": {"event_id": _STR}, "required": ["event_id"]},
)
async def calendar_read_event(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().calendar_read_event(event_id=args["event_id"])
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


@tool(
    "calendar_create_event",
    "Propose creating a single, non-recurring calendar event. This does NOT "
    "create anything: apm_connectors records it as a pending action "
    "requiring a separate human approval first. start/end are RFC3339 "
    "datetimes.",
    {
        "type": "object",
        "properties": {
            "title": _STR,
            "start": _STR,
            "end": _STR,
            "attendees": _STR_LIST,
            "location": _STR,
        },
        "required": ["title", "start", "end"],
    },
)
async def calendar_create_event(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().calendar_create_event(
            title=args["title"],
            start=args["start"],
            end=args["end"],
            attendees=args.get("attendees"),
            location=args.get("location"),
        )
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


# -- Excel ------------------------------------------------------------------


@tool(
    "excel_list_worksheets",
    "List worksheet names in the configured workbook. Read-only.",
    {"type": "object", "properties": {}},
)
async def excel_list_worksheets(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().excel_list_worksheets()
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


@tool(
    "excel_read_range",
    "Read a cell range from a worksheet. Omit sheet_name/address for the "
    "workbook's first sheet and whole used range. Read-only.",
    {"type": "object", "properties": {"sheet_name": _STR, "address": _STR}},
)
async def excel_read_range(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().excel_read_range(
            sheet_name=args.get("sheet_name"), address=args.get("address")
        )
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


@tool(
    "excel_write_range",
    "Propose overwriting a cell range with new values. This does NOT write "
    "anything: apm_connectors records it as a pending action requiring a "
    "separate human approval first.",
    {
        "type": "object",
        "properties": {
            "sheet_name": _STR,
            "address": _STR,
            "values": _VALUES_GRID,
        },
        "required": ["sheet_name", "address", "values"],
    },
)
async def excel_write_range(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().excel_write_range(
            sheet_name=args["sheet_name"], address=args["address"], values=args["values"]
        )
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


# -- Drive documents ----------------------------------------------------------


@tool(
    "drive_list_files",
    "List files in the configured Drive folder, optionally filtered by "
    "name. Read-only.",
    {
        "type": "object",
        "properties": {"name_contains": _STR, "max_results": _INT},
    },
)
async def drive_list_files(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().drive_list_files(
            name_contains=args.get("name_contains"),
            max_results=args.get("max_results", 20),
        )
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


@tool(
    "drive_read_file",
    "Download one file's contents (base64) from the configured Drive "
    "folder by id. Read-only.",
    {"type": "object", "properties": {"file_id": _STR}, "required": ["file_id"]},
)
async def drive_read_file(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().drive_read_file(file_id=args["file_id"])
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


@tool(
    "drive_upload_file",
    "Propose uploading a new file (base64 content) into the configured "
    "Drive folder. This does NOT upload anything: apm_connectors records "
    "it as a pending action requiring a separate human approval first.",
    {
        "type": "object",
        "properties": {
            "name": _STR,
            "content_base64": _STR,
            "mime_type": _STR,
        },
        "required": ["name", "content_base64", "mime_type"],
    },
)
async def drive_upload_file(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().drive_upload_file(
            name=args["name"],
            content_base64=args["content_base64"],
            mime_type=args["mime_type"],
        )
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


@tool(
    "drive_update_file",
    "Propose replacing an existing Drive file's contents (base64). This "
    "does NOT write anything: apm_connectors records it as a pending "
    "action requiring a separate human approval first.",
    {
        "type": "object",
        "properties": {"file_id": _STR, "content_base64": _STR},
        "required": ["file_id", "content_base64"],
    },
)
async def drive_update_file(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().drive_update_file(
            file_id=args["file_id"], content_base64=args["content_base64"]
        )
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


# -- Salesforce ---------------------------------------------------------------


@tool(
    "salesforce_query_records",
    "Run a SOQL query against Salesforce (cap result size with SOQL's own "
    "LIMIT clause). Read-only.",
    {"type": "object", "properties": {"soql": _STR}, "required": ["soql"]},
)
async def salesforce_query_records(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().salesforce_query_records(soql=args["soql"])
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


@tool(
    "salesforce_get_record",
    "Read a single Salesforce record by object name and id. Read-only.",
    {
        "type": "object",
        "properties": {"object_name": _STR, "record_id": _STR},
        "required": ["object_name", "record_id"],
    },
)
async def salesforce_get_record(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().salesforce_get_record(
            object_name=args["object_name"], record_id=args["record_id"]
        )
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


@tool(
    "salesforce_create_record",
    "Propose creating a Salesforce record. This does NOT create anything: "
    "apm_connectors records it as a pending action requiring a separate "
    "human approval first.",
    {
        "type": "object",
        "properties": {"object_name": _STR, "fields": _FIELDS_OBJ},
        "required": ["object_name", "fields"],
    },
)
async def salesforce_create_record(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().salesforce_create_record(
            object_name=args["object_name"], fields=args["fields"]
        )
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


@tool(
    "salesforce_update_record",
    "Propose updating a Salesforce record's fields (e.g. an Opportunity's "
    "StageName/CloseDate). This does NOT write anything: apm_connectors "
    "records it as a pending action requiring a separate human approval "
    "first.",
    {
        "type": "object",
        "properties": {
            "object_name": _STR,
            "record_id": _STR,
            "fields": _FIELDS_OBJ,
        },
        "required": ["object_name", "record_id", "fields"],
    },
)
async def salesforce_update_record(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().salesforce_update_record(
            object_name=args["object_name"],
            record_id=args["record_id"],
            fields=args["fields"],
        )
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


# -- Jira -----------------------------------------------------------------------


@tool(
    "jira_search_issues",
    "Run a JQL search (cap result size with max_results). JQL needs a real "
    "restriction clause, e.g. 'project = OPS AND status = \"In Progress\"'. "
    "Read-only.",
    {
        "type": "object",
        "properties": {"jql": _STR, "max_results": _INT},
        "required": ["jql"],
    },
)
async def jira_search_issues(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().jira_search_issues(
            jql=args["jql"], max_results=args.get("max_results")
        )
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


@tool(
    "jira_get_issue",
    "Read a single Jira issue by key. Read-only.",
    {"type": "object", "properties": {"issue_key": _STR}, "required": ["issue_key"]},
)
async def jira_get_issue(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().jira_get_issue(issue_key=args["issue_key"])
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


@tool(
    "jira_create_issue",
    "Propose creating a Jira issue, e.g. to route follow-up work to a "
    "team/partner's actual project (target a real Jira project/component, "
    "not an ad hoc issue). This does NOT create anything: apm_connectors "
    "records it as a pending action requiring a separate human approval "
    "first. `fields` is the Jira `fields` payload as-is, e.g. "
    "{\"project\": {\"key\": \"OPS\"}, \"summary\": \"...\", "
    "\"issuetype\": {\"name\": \"Task\"}}.",
    {"type": "object", "properties": {"fields": _FIELDS_OBJ}, "required": ["fields"]},
)
async def jira_create_issue(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().jira_create_issue(fields=args["fields"])
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


@tool(
    "jira_update_issue",
    "Propose updating a Jira issue's fields. This does NOT write anything: "
    "apm_connectors records it as a pending action requiring a separate "
    "human approval first.",
    {
        "type": "object",
        "properties": {"issue_key": _STR, "fields": _FIELDS_OBJ},
        "required": ["issue_key", "fields"],
    },
)
async def jira_update_issue(args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await get_client().jira_update_issue(
            issue_key=args["issue_key"], fields=args["fields"]
        )
    except ConnectorError as exc:
        return _err(str(exc))
    return _ok(result)


ALL_TOOLS = [
    gmail_search_emails,
    gmail_read_message,
    gmail_send_email,
    calendar_search_events,
    calendar_read_event,
    calendar_create_event,
    excel_list_worksheets,
    excel_read_range,
    excel_write_range,
    drive_list_files,
    drive_read_file,
    drive_upload_file,
    drive_update_file,
    salesforce_query_records,
    salesforce_get_record,
    salesforce_create_record,
    salesforce_update_record,
    jira_search_issues,
    jira_get_issue,
    jira_create_issue,
    jira_update_issue,
]

apm_connectors_server = create_sdk_mcp_server(
    name="apm_connectors", version="0.1.0", tools=ALL_TOOLS
)
