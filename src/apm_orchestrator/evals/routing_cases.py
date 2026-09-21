"""The golden dataset for the Supervisor routing eval.

Grew out of scripts/test_supervisor_routing.py's original 3 hand-picked
cases (see FAILURES_AND_LESSONS_LEARNED.md section 9), which proved the
Supervisor's routing claims hold on one clean case per agent plus one
adversarial one. This is the same idea scaled up and organized by what
each case is actually testing, so a routing regression in a specific
class of request (not just "routing broke somehow") is diagnosable from
the label alone.

Categories:
- clear:        an unambiguous request for one agent, no trap in the
                wording. These must always route correctly -- a failure
                here means basic routing is broken, not a subtle miss.
- adversarial:  correct routing is still unambiguous by the Supervisor's
                own stated rules (see supervisor.py's SYSTEM_PROMPT --
                "existing customer" vs "brand-new customer" is the one
                fact that decides it), but the wording is deliberately
                chosen to tempt keyword-matching (mentions the *other*
                agent's vocabulary, a connector name, or a word like
                "renewal"/"onboarding" used in a sense that doesn't
                apply). These must also always route correctly; the
                whole point of this category is that "sounds like X" and
                "is X" diverge.
- out_of_scope: the business intent isn't Order-Renewal or
                Customer-Onboarding at all (e.g. churn, support, IT).
                The Supervisor should call neither delegate and say so --
                per its own system prompt, "no reasoning of its own
                beyond its stated job."
- ambiguous:    genuinely underspecified or genuinely torn between both
                agents' definitions -- there is no single correct
                delegate, only a decision to observe. Declining to route
                (or routing either way with a stated rationale) both
                count as reasonable; these never gate pass/fail, the
                same way the original script's case 3 was "OBSERVE," not
                "PASS"/"FAIL". Kept small and reviewed by a human
                periodically, not scaled up like the other categories --
                a big pile of unscoreable cases doesn't add eval
                coverage, just chat noise.

Expanding this dataset: add a case, pick a category (adversarial cases
should name the specific trap being tested in a comment), and if it's a
hard-expected case make sure the correct delegate follows directly from
SYSTEM_PROMPT's actual rules -- not from a judgment call this dataset
would be making up on the Supervisor's behalf.
"""

from __future__ import annotations

from dataclasses import dataclass

ORDER_RENEWAL = "order_renewal"
CUSTOMER_ONBOARDING = "customer_onboarding"


@dataclass(frozen=True)
class RoutingCase:
    label: str
    category: str
    prompt: str
    # The one delegate expected, or None for a genuinely ambiguous case
    # (see module docstring's "ambiguous" category) -- never asserted on.
    expected_delegate: str | None
    # True for an out-of-scope case: hard-asserts delegates_called == [],
    # distinct from `expected_delegate is None`'s "don't assert" meaning.
    expect_none: bool = False


CASES: list[RoutingCase] = [
    # -- clear: Order-Renewal --------------------------------------------
    RoutingCase(
        "clear renewal: forwarded customer email",
        "clear",
        "Acme Corp's license is coming up for renewal next month and "
        "they emailed asking to extend their existing contract.",
        ORDER_RENEWAL,
    ),
    RoutingCase(
        "clear renewal: expiring subscription",
        "clear",
        "Northwind Traders' annual subscription expires in three weeks. "
        "Please start the renewal process.",
        ORDER_RENEWAL,
    ),
    RoutingCase(
        "clear renewal: schedule the renewal call",
        "clear",
        "We need to get a renewal call on the calendar with Initech "
        "before their current term lapses.",
        ORDER_RENEWAL,
    ),
    RoutingCase(
        "clear renewal: upsell tier change on an existing contract",
        "clear",
        "Globex wants to renew but move up to the Enterprise tier this "
        "time -- same existing account, higher-value renewal.",
        ORDER_RENEWAL,
    ),
    RoutingCase(
        "clear renewal: reversing an opt-out",
        "clear",
        "Umbrella Corp told us last week they wouldn't renew, but they "
        "just called back and want to go ahead with the renewal after "
        "all -- same contract as before.",
        ORDER_RENEWAL,
    ),
    # -- clear: Customer-Onboarding ---------------------------------------
    RoutingCase(
        "clear onboarding: brand-new closed-won deal",
        "clear",
        "We just closed-won a brand-new deal with Initrode Corp -- kick "
        "off their onboarding.",
        CUSTOMER_ONBOARDING,
    ),
    RoutingCase(
        "clear onboarding: first kickoff call for a new logo",
        "clear",
        "Wonka Industries signed yesterday as a brand-new customer. "
        "Get their kickoff call and welcome packet started.",
        CUSTOMER_ONBOARDING,
    ),
    RoutingCase(
        "clear onboarding: partner-referred new account",
        "clear",
        "A new customer, Stark Industries, just came in through our "
        "partner program -- first contract ever with them. Start "
        "onboarding.",
        CUSTOMER_ONBOARDING,
    ),
    RoutingCase(
        "clear onboarding: enterprise deal needs a welcome email",
        "clear",
        "Just closed a brand-new enterprise deal with Hooli. They've "
        "never been a customer before -- send the welcome email and "
        "open an onboarding ticket.",
        CUSTOMER_ONBOARDING,
    ),
    RoutingCase(
        # A trap on the surface ("conversion" sounds renewal-adjacent),
        # but the deciding fact per SYSTEM_PROMPT is "brand-new
        # customer's kickoff," and a trial has never been a paid
        # contract -- this is that customer's first one.
        "clear onboarding: trial-to-paid is still a first contract",
        "clear",
        "Pied Piper just converted from their trial to a first paid "
        "contract -- they've never been a paying customer before. Kick "
        "off onboarding.",
        CUSTOMER_ONBOARDING,
    ),
    # -- adversarial: business intent vs. surface wording ------------------
    RoutingCase(
        # Trap: mentions Salesforce and Jira by name. Correct routing
        # doesn't depend on which connectors are mentioned at all.
        "adversarial: connector names mentioned, still a renewal",
        "adversarial",
        "Can you check Salesforce and Jira for Acme Corp -- their "
        "contract renewal is due and I want to make sure there's no "
        "blocking ticket before we extend it.",
        ORDER_RENEWAL,
    ),
    RoutingCase(
        # Trap: "renewal" appears, but it describes an unpaid trial
        # extension for a customer with no existing contract -- not an
        # Order-Renewal-shaped renewal at all.
        "adversarial: 'renewal' of a free trial, not a contract",
        "adversarial",
        "Aperture Science's free trial is expiring and they want a "
        "renewal of the trial -- they're not a paying customer yet, "
        "this would be their first real contract once it converts. "
        "Let's get them started.",
        CUSTOMER_ONBOARDING,
    ),
    RoutingCase(
        # Trap: an existing customer's *new* product line, which reads
        # like "new" business but is still the same existing account and
        # contract relationship.
        "adversarial: existing customer's new product line is still a renewal",
        "adversarial",
        "Massive Dynamic (an existing customer of five years) wants to "
        "add a new product line to their current contract at renewal "
        "time.",
        ORDER_RENEWAL,
    ),
    RoutingCase(
        # Trap: explicitly names both agents' territory in the same
        # sentence, but the account status ("existing," "current
        # contract") settles it.
        "adversarial: names both agents, existing account settles it",
        "adversarial",
        "Not sure if this is a renewal or an onboarding case -- Cyberdyne "
        "is an existing customer and their current contract is up for "
        "renewal next month.",
        ORDER_RENEWAL,
    ),
    RoutingCase(
        # Trap: "contract" and "account" (renewal-flavored words) used
        # for a customer's very first one.
        "adversarial: 'contract'/'account' language for a first-time customer",
        "adversarial",
        "Soylent Corp signed their very first contract with us this "
        "week -- brand new account, no prior history. Set up their "
        "onboarding.",
        CUSTOMER_ONBOARDING,
    ),
    # -- out_of_scope: neither agent -----------------------------------
    RoutingCase(
        "out of scope: churn/save-the-account request",
        "out_of_scope",
        "Our biggest account, Gringotts Bank, is threatening to cancel "
        "next quarter -- can you do something to save them?",
        None,
        expect_none=True,
    ),
    RoutingCase(
        "out of scope: IT support request",
        "out_of_scope",
        "I'm locked out of my email, can you reset my password?",
        None,
        expect_none=True,
    ),
    RoutingCase(
        "out of scope: billing dispute unrelated to renewal timing",
        "out_of_scope",
        "A customer is disputing a duplicate charge on last month's "
        "invoice and wants a refund -- nothing to do with their "
        "renewal date.",
        None,
        expect_none=True,
    ),
    # -- ambiguous: no single correct answer, observed not asserted ----
    RoutingCase(
        # The original adversarial case from scripts/test_supervisor_routing.py
        # -- kept as-is (see FAILURES_AND_LESSONS_LEARNED.md section 9):
        # an existing customer's expansion deal, phrased entirely in
        # Customer-Onboarding's own vocabulary. Live-verified once to
        # make the Supervisor decline rather than guess; not strict
        # enough a fact pattern to hard-assert on every model version.
        "ambiguous: existing customer, onboarding-shaped requested actions",
        "ambiguous",
        "Acme Corp, an existing customer, just closed-won an expansion "
        "deal for a new product line. Schedule a kickoff call and send "
        "them a welcome email for the new product.",
        None,
    ),
    RoutingCase(
        "ambiguous: account status itself is unstated",
        "ambiguous",
        "This customer wants a kickoff call scheduled for their "
        "expanded contract.",
        None,
    ),
]
