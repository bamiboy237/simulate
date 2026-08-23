# src/app/domain/reference_workflows/

## Responsibility
Reference multi-agent workflows providing end-to-end examples for CI triage, insurance claims denial, and incident response.

## Key Sub-Packages
- `ci_triage/`: CI failure log parsing, root cause analysis, and PR fix recommendation.
- `claims_denial/`: Medical/insurance claim code evaluation and denial appeal review.
- `incident_response/`: Sentry/PagerDuty incident triage and mitigation playbooks.

## Integration
- **Consumed by:** User simulator scenarios, regression test suites.
