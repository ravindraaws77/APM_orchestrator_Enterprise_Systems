"""Is the Supervisor's confidence signal actually calibrated?

Confidence only earns its place if a "low" call is meaningfully more
likely to be wrong than a "high" one. This computes that directly from
reviewed ground truth (see review_routing_log.py) -- not from the eval
dataset's own category labels, which only check "does the model agree
with the dataset author," a weaker claim. If the wrong-rate on "low"
isn't clearly higher than on "high", the confidence field isn't
carrying real signal yet, whatever the eval harness says.

Usage:
    python scripts/calibration_report.py
"""

from __future__ import annotations

import asyncio
import sys

# psycopg's async mode needs a selector-based event loop; Windows'
# asyncio default (ProactorEventLoop) isn't compatible with it -- same
# fix as run_case.py/show_case.py/poller.py/cli.py. No effect on other
# platforms.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from apm_orchestrator.config import load_settings
from apm_orchestrator.db import SupervisorRoutingLog

# Below this many reviewed rows in a bucket, a wrong-rate is too noisy
# to act on -- reported, but not used to draw a conclusion either way.
MIN_REVIEWED_FOR_VERDICT = 10


def _fmt_rate(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.0%}"


async def main() -> None:
    settings = load_settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is required")

    log = SupervisorRoutingLog(settings.database_url)
    await log.setup()
    report = await log.calibration_report()

    print("=== Confidence calibration ===\n")
    for bucket in ("high", "low"):
        counts = report.get(bucket, {"reviewed": 0, "wrong": 0, "wrong_rate": None})
        rate = _fmt_rate(counts["wrong_rate"])
        print(f"{bucket:>4}-confidence: {counts['wrong']}/{counts['reviewed']} wrong ({rate})")

    high = report.get("high", {"reviewed": 0, "wrong_rate": None})
    low = report.get("low", {"reviewed": 0, "wrong_rate": None})

    print()
    if high["reviewed"] < MIN_REVIEWED_FOR_VERDICT or low["reviewed"] < MIN_REVIEWED_FOR_VERDICT:
        print(
            f"Not enough reviewed data for a verdict yet -- need at least "
            f"{MIN_REVIEWED_FOR_VERDICT} reviewed rows in each bucket "
            f"(have {high['reviewed']} high, {low['reviewed']} low). "
            "Keep sampling with review_routing_log.py."
        )
    elif low["wrong_rate"] > high["wrong_rate"]:
        print(
            f"Calibrated: low-confidence routings are wrong more often "
            f"({_fmt_rate(low['wrong_rate'])}) than high-confidence ones "
            f"({_fmt_rate(high['wrong_rate'])}). The confidence signal is worth trusting."
        )
    else:
        print(
            f"NOT calibrated: low-confidence wrong-rate ({_fmt_rate(low['wrong_rate'])}) "
            f"isn't higher than high-confidence ({_fmt_rate(high['wrong_rate'])}). "
            "Flagging on confidence right now doesn't reliably surface the routings "
            "that are actually wrong -- worth revisiting SYSTEM_PROMPT's calibration "
            "guidance before leaning on this signal further."
        )


if __name__ == "__main__":
    asyncio.run(main())
