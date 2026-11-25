"""Strands Agents spike: the renewal-readiness triage agent.

This is a framework evaluation, not a registered business-process agent.
It runs one bounded job (renewal-readiness triage for a single account)
on AWS's open-source Strands Agents SDK instead of the Claude Agent SDK,
so the two frameworks can be compared on the same connectors, the same
policy and the same guardrails. The Supervisor does not delegate to it.

Install with the ``strands`` extra (plus ``postgres``, because it reuses
the Order-Renewal case graph's live-verified account-matching helpers):

    pip install -e ".[dev,postgres,strands]"

See ``README.md``'s "Strands Agents spike" section for what it shows and
how the two frameworks compare.
"""
