"""Thin CLI entry point for the Supervisor routing eval.

The original version of this script (see FAILURES_AND_LESSONS_LEARNED.md
section 9) held 3 hand-picked cases and the tracing logic inline. Both
now live in apm_orchestrator.evals -- routing_cases.py for the golden
dataset (grown to cover clear/adversarial/out-of-scope/ambiguous cases,
not just one of each) and run_supervisor_routing_eval.py for the runner
-- so the same dataset backs both this manual entry point and the CI
eval job (.github/workflows/eval.yml) without drifting apart.

Needs ANTHROPIC_API_KEY. Does NOT need DATABASE_URL or live connector
credentials -- see run_supervisor_routing_eval's module docstring for
why (the delegated agents' own downstream execution is stubbed; only
the routing decision itself is under test).

Usage:
    python scripts/test_supervisor_routing.py
    python scripts/test_supervisor_routing.py --output results.json
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from apm_orchestrator.evals.run_supervisor_routing_eval import main

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=None, help="write JSON results to this path")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.output)))
