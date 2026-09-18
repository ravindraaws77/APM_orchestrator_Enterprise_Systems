from apm_orchestrator.agents.customer_onboarding.policy import load_policy
from apm_orchestrator.agents.order_renewal.policy import load_policy as load_order_renewal_policy


def test_loads_bundled_default_policy():
    policy = load_policy()

    assert "New Customer" in policy.salesforce_detect_soql_template
    assert "{account_name}" in policy.salesforce_detect_soql_template
    assert "{account_name}" in policy.salesforce_detect_fallback_soql_template
    assert "LIKE" in policy.salesforce_detect_fallback_soql_template
    assert policy.onboarding_window_days > policy.escalation_threshold_days > 0
    assert "{account_id}" in policy.primary_contact_soql_template
    assert "{account_name}" in policy.blocking_jql_template


def test_record_update_targets_onboarding_status_custom_field():
    policy = load_policy()

    fields = policy.record_update_fields
    # Salesforce requires custom fields to carry the __c suffix in their
    # API name regardless of UI label -- see CUSTOMER_ONBOARDING_CONTRACT.md.
    assert any(name.endswith("__c") for name in fields)


def test_onboarding_tracking_and_blocking_tickets_share_order_renewal_project_key():
    """Deliberately points at the same real Jira project key as
    order_renewal/policy.yaml -- one real key fills in both agents at
    once. See policy.yaml's own comments for the rationale."""
    policy = load_policy()
    order_renewal_policy = load_order_renewal_policy()

    assert policy.onboarding_tracking_project_key == order_renewal_policy.raw["follow_up_routing"]["default_project_key"]
    assert f"project = {policy.onboarding_tracking_project_key} " in policy.blocking_jql_template
    assert f"project = {policy.onboarding_tracking_project_key} " in order_renewal_policy.blocking_jql_template
