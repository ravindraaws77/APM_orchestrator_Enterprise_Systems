"""Document extraction as its own A2A agent (roadmap Phase 6.3).

A specialist agent that turns a document (today: a purchase order or
signed renewal quote) into structured fields with a per-field confidence
score, exposed over the A2A protocol so any business agent -- or another
team's agent -- calls it as a peer instead of re-implementing extraction
in-process.

How it fits the two-repo split:

- Extraction is reasoning, so it lives on this side, never in
  `apm_connectors`. The calling business agent fetches the document
  bytes from `apm_connectors`' read routes (e.g. `POST /tools/drive/read`)
  and hands them over; this agent holds no connector credentials and
  can't read or write any external system itself.
- Its output is *untrusted data* to the caller. `client.py` re-validates
  every artifact against `schema.py` before a case graph acts on it, and
  nothing it returns is ever spliced into a prompt as instructions.
- A low-confidence extraction moves the A2A task to `input-required` --
  a human-review checkpoint for the *extracted values*. That is not the
  write-approval gate: whatever write a reviewed extraction leads to
  still goes through `apm_connectors`' own approval queue.

Modules:
    schema.py    field definitions, JSON schema, validation, review rules
    extractor.py the Claude Messages API call that does the extraction
    server.py    the A2A server (Agent Card + JSON-RPC endpoint)
    client.py    the A2A client business agents use
"""
