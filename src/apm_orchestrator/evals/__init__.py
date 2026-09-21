"""Golden-dataset evals for this repo's LLM-based decisions -- distinct
from tests/, which covers deterministic code (case-graph nodes, policy
loading) with mocked clients. An eval here calls the real Claude API and
is judged by pass-rate against a versioned dataset, not by
assert-equal-on-one-input.

Today: Supervisor routing (routing_cases.py, run_supervisor_routing_eval.py).
"""
