from apm_orchestrator.agents.order_renewal.policy import load_policy


def test_loads_bundled_default_policy():
    policy = load_policy()

    assert "renewal" in policy.gmail_query
    assert policy.renewal_window_days > policy.escalation_threshold_days > 0
    assert "{account_name}" in policy.salesforce_lookup_soql_template
    assert "{account_name}" in policy.blocking_jql_template


def test_route_for_falls_back_to_default_project():
    policy = load_policy()

    assert policy.route_for(None) == policy.raw["follow_up_routing"]["default_project_key"]
    assert policy.route_for("no-such-tag") == policy.raw["follow_up_routing"][
        "default_project_key"
    ]


def test_route_for_matches_configured_tag():
    policy = load_policy()

    # Asserts route_for returns the matched route's own key, not merely
    # a fallback that happens to coincide with it -- doesn't assume it
    # differs from default_project_key, since a real deployment running
    # one Jira project for everything sets every route to the same key.
    matched = policy.route_for("enterprise")
    assert matched == policy.raw["follow_up_routing"]["routes"][1]["project_key"]
