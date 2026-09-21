from pathlib import Path

from apm_orchestrator.agents.order_renewal.policy import load_policy


def test_loads_bundled_default_policy():
    policy = load_policy()

    assert "renewal" in policy.gmail_query
    assert policy.renewal_window_days > policy.escalation_threshold_days > 0
    assert "{account_name}" in policy.salesforce_lookup_soql_template
    assert "{account_name}" in policy.blocking_jql_template


def test_policy_version_is_stable_for_the_same_file_content():
    """A content hash, not a human-maintained version number -- see
    policy.py's own comment on why (a forgotten version bump is exactly
    the failure mode this avoids). Loading the identical bundled policy
    twice must give the identical version."""
    first = load_policy()
    second = load_policy()

    assert first.policy_version == second.policy_version
    assert len(first.policy_version) == 12


def test_policy_version_changes_when_the_file_content_changes(tmp_path: Path):
    original = tmp_path / "policy.yaml"
    original.write_text("detection:\n  gmail_query: 'a'\n")
    changed = tmp_path / "policy_changed.yaml"
    changed.write_text("detection:\n  gmail_query: 'b'\n")

    assert load_policy(original).policy_version != load_policy(changed).policy_version


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
