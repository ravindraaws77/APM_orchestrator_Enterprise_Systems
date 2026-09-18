from apm_orchestrator.agents.customer_onboarding.policy import load_policy


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


def test_onboarding_tracking_and_blocking_tickets_share_order_renewal_placeholders():
    """Deliberately points at the same REPLACE_WITH_... placeholders as
    order_renewal/policy.yaml -- one real Jira project key fills in both
    agents at once. See policy.yaml's own comments for the rationale."""
    policy = load_policy()

    assert policy.onboarding_tracking_project_key == "REPLACE_WITH_YOUR_DEFAULT_JIRA_PROJECT_KEY"
    assert "REPLACE_WITH_YOUR_SUPPORT_PROJECT_KEY" in policy.blocking_jql_template
