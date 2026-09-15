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

    matched = policy.route_for("enterprise")
    assert matched == "ENTSUCCESS"
