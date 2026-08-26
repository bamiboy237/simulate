# Reference Workflows

This package contains realistic reference workflows for the simulation platform. Each workflow defines a domain with state entities, tool contracts, baseline and candidate configurations, and deterministic offline fixture data.

## Workflow catalog

### Subpackage workflows

| Workflow | Directory | Domain | Comparison variable |
| --- | --- | --- | --- |
| Incident-response on-call agent | [`incident_response/`](file:///Users/king/Desktop/simulate/src/app/domain/reference_workflows/incident_response/) | SRE runbook alert handling and paging | Runbook retrieval strategy (keyword vs semantic) |
| CI failure triage agent | [`ci_triage/`](file:///Users/king/Desktop/simulate/src/app/domain/reference_workflows/ci_triage/) | Flaky test classification and fix branches | Classifier model (gpt-4.1-mini vs gpt-5.2) |
| Claim denial management agent | [`claims_denial/`](file:///Users/king/Desktop/simulate/src/app/domain/reference_workflows/claims_denial/) | Healthcare appeal drafting and policy grounding | Appeal autonomy level (human confirmation vs automatic submission) |

### Flat fixture workflows

| Workflow | Module | Domain | Comparison variable |
| --- | --- | --- | --- |
| Returns resolution agent | [`returns_resolution.py`](file:///Users/king/Desktop/simulate/src/app/domain/reference_workflows/returns_resolution.py) | E-commerce returns and refunds | Refund confirmation gate |
| HR onboarding coordinator | [`onboarding.py`](file:///Users/king/Desktop/simulate/src/app/domain/reference_workflows/onboarding.py) | Employee onboarding and compliance | Checklist selection source |
| Banking dispute resolution | [`disputes.py`](file:///Users/king/Desktop/simulate/src/app/domain/reference_workflows/disputes.py) | Disputed transaction handling | Minimum required evidence sources |

---

## Shared architectural design

Every reference workflow adheres to these principles:

- **Stateful system:** Each workflow defines versioned state records with explicit state machines. The simulation environment seeds an ephemeral in-memory copy for each run.
- **Tool boundaries:** Tools are categorized as safe (read-only) or sensitive (write or external effects). Sensitive actions require explicit authorization or confirmation gates.
- **Single comparison variable:** Experiments test exactly one declared configuration difference between baseline and candidate runs.
- **Deterministic fixtures:** Modules use only standard library components and Pydantic. Repeated imports produce identical UUIDs and content hashes.

---

## Offline verification

To verify that all reference workflow fixtures load and validate deterministically, run:

```bash
uv run python -c "import app.domain.reference_workflows.incident_response.fixtures"
uv run python -c "import app.domain.reference_workflows.ci_triage.fixtures"
uv run python -c "import app.domain.reference_workflows.claims_denial.fixtures"
uv run python -c "import app.domain.reference_workflows.returns_resolution"
uv run python -c "import app.domain.reference_workflows.onboarding"
uv run python -c "import app.domain.reference_workflows.disputes"
```

Each import validates scenario definitions and calculates reproducible content hashes.
