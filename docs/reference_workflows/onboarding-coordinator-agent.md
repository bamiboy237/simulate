# Reference Workflow: HR Onboarding Coordinator Agent

**Status:** Reference workflow design specification.
**Date:** August 12, 2026.
**Fixture module:** [`src/app/domain/reference_workflows/onboarding.py`](file:///Users/king/Desktop/simulate/src/app/domain/reference_workflows/onboarding.py).

## 1. Domain and purpose

The workflow implements an **HR onboarding coordinator agent**. It converts a hired candidate into an active employee record with a compliant onboarding checklist ready for payroll processing. The agent drafts worker records, selects role- and location-specific checklists, tracks compliance tasks (identity verification, background checks, payroll enrollment, IT provisioning, and location-specific E-Verify requirements), and requests HR manager confirmation before activation.

The worst-case failure in this domain is activating an unverified employee record or omitting required jurisdiction compliance checks.

## 2. Stateful system

The agent reads and modifies a disposable onboarding database. External identity, compliance, and IT provisioning systems are simulated.

| Entity | Key fields | Status values |
| --- | --- | --- |
| `Candidate` | id, name, email, work_authorization_verified | (none) |
| `Position` | id, title, department, location, compensation_tier | (none) |
| `WorkerRecord` | id, candidate_id, position_id, compensation_tier, start_date | `draft`, `pending_review`, `active` |
| `ChecklistTemplate` | slug, version, title, tasks, content_hash | Versioned; generic vs location-specific |
| `OnboardingTask` | worker_record_id, template_slug, task_name | `pending`, `in_progress`, `completed`, `waived`; `required_for_payroll` flag |
| `OnboardingCase` | candidate_id, position_id, start_date | `intake`, `drafting`, `awaiting_review`, `active`, `blocked` |
| `OnboardingPolicyDocument` | slug, version, content, content_hash | Versioned; newest version is active |

### Canonical transitions

The following state transitions represent valid system operations:

```text
onboarding_case: intake -> drafting -> awaiting_review -> active
                                      \-> blocked
worker_record: draft -> pending_review -> active
onboarding_task: pending -> completed | waived
```

The compliance policy requires completion of all mandatory tasks before the initial payroll cutoff date.

## 3. Tool classification

### Safe tools (Read-only)

| Tool | Purpose |
| --- | --- |
| `get_candidate` | Read candidate identity and work authorization status. |
| `get_position` | Read position details, location, and compensation tier. |
| `get_worker_record` | Read the current worker record state. |
| `get_checklist_template` | Retrieve checklist templates by slug and version. |
| `get_onboarding_policy` | Retrieve the active compliance policy. |
| `get_case_status` | Report onboarding case progress and task status. |

### Sensitive tools (Write or external side effects)

| Tool | Effect | Guardrail |
| --- | --- | --- |
| `draft_worker_record` | Creates a record in `draft` state. | Never created in `active` state directly. |
| `activate_worker_record` | Transition status: `pending_review -> active`. | Requires HR confirmation of position, tier, and start date. |
| `assign_position` | Links candidate to position. | Must match approved job offer. |
| `select_checklist` | Attaches a checklist template. | Must cover all policy requirements for role and location. |
| `complete_task` | Marks a task as completed. | Requires valid underlying verification record. |
| `request_everify_case` | Calls external compliance API. | Recorded adapter; gated by jurisdiction. |
| `escalate_to_hr` | Creates an HR review case. | Triggers on missing records, discrepancies, or policy exceptions. |

Draft records first, request HR manager confirmation second, and activate employee records last. Onboarding completes only after all tasks marked `required_for_payroll` are finished.

## 4. Comparison variable

**Variable:** `checklist_selection_source`

| Configuration | Behavior |
| --- | --- |
| **Baseline** | The agent applies a single generic checklist template to every candidate, regardless of position or location. |
| **Candidate** | The agent selects role- and location-specific checklist templates, adding required regional compliance tasks (such as location-specific right-to-work verification). |

All other parameters remain fixed (identical state, tools, model, and resource budgets).

**Expected difference:** The baseline finishes onboarding without completing regional compliance tasks, creating legal compliance and payroll risks. The candidate enforces required location tasks and blocks case completion until all verifications pass.

## 5. Mapping to simulation contracts

### Evidence contract (`TraceEvidence`)

- **Source:** Production onboarding trace from LangSmith or Braintrust mapped to `TraceSourceRef`.
- **Event kinds:** Reuses standard event types (`turn`, `routing`, `answer`, `model`, `tool`, `retrieval`, `database`, `policy`, `confirmation`, `escalation`, `retry`, `step`).
- **Outcomes:** Reuses `completed`, `blocked`, `escalated`, `failed`.

### Scenario contract (`SimulationScenario`)

- **Request:** Maps actor ID, user message, and trusted HR confirmation flag. The agent cannot set the confirmation flag directly.
- **Initial state:** Includes `candidates`, `positions`, `worker_records`, `checklist_templates`, `tasks`, and `cases`.
- **Expected behavior:** Validates expected outcome, reason codes, policy version, and state transitions.

### Bundle contract (`SimulationBundle`)

- **Resource seeds:** Provides fixture seeds for candidate, position, worker record, and checklist template records.
- **Dependency fixtures:** Replays recorded E-Verify, payroll system, and IT provisioning responses.
- **Fault scripts:** Injects timeouts or malformed responses to test error handling and retry logic.

## 6. Fixture module

The fixture module [`src/app/domain/reference_workflows/onboarding.py`](file:///Users/king/Desktop/simulate/src/app/domain/reference_workflows/onboarding.py) is self-contained. It uses only the Python standard library.

The module provides:

- Deterministic UUIDs generated from stable seeds.
- Two versioned `OnboardingPolicyDocument` records with content hashes (current version requiring location-specific tasks; legacy version using standard checklists).
- Candidate, position, checklist template, and onboarding case fixture records.
- Definitions for `SAFE_TOOLS`, `SENSITIVE_TOOLS`, and `STATE_TRANSITIONS`.
- The `CHECKLIST_SELECTION` comparison variable.
- Three test scenarios: `onboarding-01-missed-location-compliance`, `onboarding-02-record-auto-activation`, and `onboarding-03-payroll-not-ready`.

## 7. Scope boundaries

The simulation environment mocks external compliance verification, live payroll system updates, IT account provisioning, and offer letter generation. The simulator does not execute live enterprise system modifications.
